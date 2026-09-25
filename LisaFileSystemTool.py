#!/usr/bin/env python3
#####################################################################################################
# LisaFileSystemTool.py is a standalone Python tool for inspecting and modifying                    #
# Apple Lisa Office System (LOS) and Workshop disk images. It understands two image container       #
# formats (DC42 and Raw ProFile), works with both floppy and ProFile/hard-disk volumes, and works   #
# with every known Lisa OS version (LOS 1.0 through LOS 3.1, i.e. both the old "flat catalog"       #
# volumes and the newer HFS-style B-tree catalog volumes).                                          #
#                                                                                                   #
# The tool can remove file copy protection from disk images (if any is present), which makes them   #
# more universal: they are no-longer tied to a specific Lisa serial number, and thus can be run     #
# on any real and emulated Lisa.                                                                    #
# Disclaimer: given that the Lisa OS source files are open-source and free-to-use now               #
# (at https://info.computerhistory.org/apple-lisa-code), we believe that removing file copy         #
# protection causes no harm or loss to Apple.                                                       #
#                                                                                                   #
# If you want to "add" files from your host machine to a disk image, use the adjacent tool          #
# LisaFileSystemToolAddFile.py                                                                      #
#                                                                                                   #
# See the adjacent README.md file for usage.                                                        #
#                                                                                                   #
# Author: TorZidan                                                                                  #
# Date: Sept 19, 2026                                                                               #
# License: Published under the GNU General Public License v3.0.                                     #
#####################################################################################################

from datetime import datetime, timezone
from io import BufferedReader, BytesIO
from typing import BinaryIO, List
import os
import psutil
import struct
import sys

DC42_HEADER_SIZE = 0x54  # = 84 bytes
SECTOR_SIZE_IN_BYTES = 512

# Found in Lisa source file LISA_OS/OS/SOURCE-VMSTUFF.TEXT.unix.txt
# For some reason, things don't match well with real Lisa profile disk images, so I had to adjust some of the offsets in the MDDF_FIELD_DEFINITIONS below.
# The fileds marked as "verified correct" are the ones that are used by this code, and they seem to be the correct field offsets.
MDDF_FIELD_DEFINITIONS = [
    ["fsversion", 0],  # at 0x 0..1 , verified correct
    ["volid", 2],  # 2..9 data structure with name "UID" of 8 bytes
    ["volnum", 10],  # = 0x0A
    [
        "volname",
        12,
    ],  # = 0x0C = 12..45 : Pascal String of 34 bytes (1 byte length + 33 bytes of ASCII characters)
    [
        "password",
        46,
    ],  # = 0x2E = 46..79 : Pascal String of 34 bytes (1 byte length + 33 bytes of ASCII characters)
    ["init_machine_id", 80],  # = 0x50, verified correct
    ["master_machine_id", 84],  # = 0x54
    ["DT_created", 88],  # = 0x58, looks legit
    ["DT_copy_created", 92],  # = 0x5C
    ["DT_copied", 96],  # = 0x60
    ["DT_scavenged", 100],  # = 0x64
    ["copy_thread", 104],  # = 0x68
    ["firstblock", 108],  # ...
    ["lastblock", 110],
    ["lastfspage", 113],
    ["blockcount", 116],
    ["blocksize", 120],
    ["datasize", 124],
    ["cluster_size", 126],
    ["MDDFaddr", 128],
    ["MDDFsize", 132],
    ["bitmap_addr", 136],  # = 0x88, verified correct
    ["bitmap_size", 140],
    ["bitmap_bytes", 144],
    ["bitmap_pages", 146],
    ["slist_addr", 148],  # = 0x94, verified correct
    ["slist_packing", 152],  # = 0x98, verified correct
    ["slist_block_count", 154],  # = 0x9a, verified correct
    ["first_file", 156],  # = 0x9c, verified correct
    ["empty_file", 158],  # = 0x9e, verified correct
    ["maxfiles", 160],
    ["hintsize", 162],
    ["leader_offset", 164],
    ["leader_pages", 166],
    ["flabel_offset", 168],
    ["unusedi1", 170],
    ["map_offset", 172],
    ["map_size", 174],
    ["filecount", 176],  # = 0xB0, verified correct
    ["freestart", 178],
    ["unusedl1", 182],
    ["freecount", 186],  # = 0xBA, verified correct
    ["rootsnum", 190],
    ["rootmaxentries", 192],
    ["mountinfo", 194],
    ["overmount_stamp", 196],
    ["pmem_id", 204],
    ["pmem", 208],
    ["vol_scavenged", 274],
    ["tbt_copied", 275],
    ["smallmap_offset", 276],
    ["hentry_offset", 278],
    ["backup_volid", 280],
    ["flabel_size", 288],
    ["fs_overhead", 290],
    ["result_scavenge", 292],
    ["boot_code", 294],
    ["boot_environ", 296],
    ["oem_id", 298],
    ["root_page", 302],  # = 0x12E, verified correct
    ["tree_depth", 306],
    ["node_id", 308],
    ["vol_seq_no", 310],
    ["vol_mounted", 312],
]
"""
Relevant documentation:
https://bitsavers.org/pdf/apple/lisa/os/Lisa_Operating_System_Reference_Manual_Mar82.pdf (Page 10):
    On structured devices, such as disk drives, the File System maintains a
    higher level of data access built out of pages (logical names for blocks),
    label contents, and data clusters (groups of contiguous pages). Any 
    file access ultimately translates into a page access. Intermediate
    buffering is provided only when it is needed. Each page on a structured
    device is self-identifying, and the page descriptor is stored with the
    page contents to reduce the destructive impact of an I/O error. The eight
    components of the page descriptor are:
        Version number      
        Volume identifier   
        File identifier     (2 bytes, aka sector type, e.g. 0001 = MDDF)
        Amount of data on the page 
        Page name           
        Page position in the file 
        Forward link        
        Backward link       
    Each structured device has a Media Descriptor Data File (MDDF) which
    describes the various attributes of the media such as its size, page
    length, block layout, and the size of the boot area. The MDDF is
    created when the volume is initialized.
    The File System also maintains a bit tmap of which pages on the media
    are currently allocated, and a catalog of all the files on the volume.
    Each file contains a set of file hints which describe and point to
    the actual file data. The file data need not be allocated in contiguous
    pages. 
    ... then it talks about the "volume catalog", labels, logical and physical end of file, etc.


# The MDDF sector format below was derived from Lisa sources file LISA_OS/LIBS/LIBOS/libos-bless.text.unix.txt, LISA_OS/OS/SOURCE-VMSTUFF.TEXT.unix.txt :
# For some reason, these offsets do not match reality. See MDDF_FIELD_DEFINITIONS for the actual offsets I found.
Offset      Field Name     Field Type
-----------------------------------------
0            fsversion    : integer;
2            volid        : UID;     # UID is a structure of two longints (4+4 = 8 bytes)
10           volnum       : integer;
12           volname      : string [max_ename];  (max is 33)
45           password     : string [max_ename];
78           init_machine_id : longint;     (* machine initialized on *)
82           master_machine_id : longint;   (* for theft protection *)
86           DT_created   : longint;        (* original creation date *)
90           DT_copy_created : longint;     (* date THIS copy made *)
94           DT_copied    : longint;        (* date this vol was backed up *)
98           DT_scavenged : longint;
102          copy_thread  : longint; (* incremented each time on a new copy *)
             geography : record
106            firstblock : longint;    (* abs block number of first block *)
110            lastblock  : longint;    (* abs block number of last  block *)
114            lastfspage : longint;    (* abs page number of last fs page *)
             end;
118          blockcount   : longint;     (* number of blocks on disk   *)
122          blocksize    : integer;     (* bytes per block, often 512 *)
124          datasize     : integer; (* data bytes per block *)
126          cluster_size:integer;(* # of blocks per cluster on disk *)
128          MDDFaddr     : longint; (* page where MDDF starts *)
132          MDDFsize     : integer; (* size in bytes of MDDF *)
136          bitmap_addr  : longint; (* page where bit map starts *)
140          bitmap_size  : longint;(* size in bits of allocation bit map *)
144          bitmap_bytes : integer; (* size in bytes of alloc. bit map *)
146          bitmap_pages : integer; (* number of data pages for bitmap bytes *)
148=0x94     slist_addr   : longint; (* page where s-file list begins *)
152=0x98     slist_packing:integer;(* number of s_entries per block in slist *)
154=0x9a     slist_block_count:integer; (* number of data blocks in slist file *)
156          first_file   : integer;  (* fid of minimum non-initial s-file *)
158=9e       empty_file   : integer;  (* minimum fid of all unused s-file ids *)
160          maxfiles     : integer;(* max s-file index on this device *)
162          hintsize     : integer;      (* number of pages in s-file hints *)
164          leader_offset: integer;  (* page offset of leader in file hints *)
166          leader_pages : integer;  (* number of pages in file leader *)
168          flabel_offset: integer;  (* byte offset of file label in first hint page *)
170          unusedi1     : integer;  (* spare field *)
172          map_offset   : integer;  (* page offset of first file map page *)
174          map_size     : integer;  (* number of file map entries per page *)
176          filecount    : integer;  (* current number of s-files allocated *)
178          freestart    : longint; (* start of free page search in bit map *)
182          unusedl1     : longint;   (* spare field *)
186          freecount    : longint; (* number of free pages in list *)
190          rootsnum     : integer; (* s-file number of root catalog file *)
192          rootmaxentries : integer; (* maximum number of centry in root cat *)
194 (enum is backed by an integer)          mountinfo : mountstate;   (* info about mount state of volume *)
196          overmount_stamp : UID; (* UID of temp overmount op *)
204          pmem_id : longint;     (* machine ID for this copy of param mem *)
208 (rec_param_mem is 65 bytes)                  pmem : rec_param_mem; (* temp parameter memory *)  
273=0x111    vol_scavenged: boolean;  (* volume modified by scavenger *)
274          tbt_copied   : boolean;     (* set on newly tbt copied disk vol *)
275          smallmap_offset : integer;  (* byte offset of fmap in hint page *)
277          hentry_offset: integer;  (* byte offset of hentry in hint page *)
279          backup_volid : UID;   (* volume that this volume backs-up *)
287          flabel_size  : integer;  (* byte size of file label *)
289          fs_overhead  : integer;  (* per-volume file system space overhead *)
291          result_scavenge : integer;  (* result of last scavenge *)
293          boot_code    : integer;  (* reserved for future use *)
295          boot_environ : integer;  (* reserved for future use *)
           end;

Note: text files (names ending in ".TEXT") are dumped as plain host text by the
"dump" command: the 1024-byte on-disk header page and the null page padding are
stripped, and the Lisa CR (0D) line endings are converted to host "\n" (see
lisa_text_file_to_host_text()), so a dumped text file can be displayed and edited
directly on the host.
"""


class InMemoryFileSystem:
    """Reads the whole file in memory and provides a _file read-only handler than can read from it as if it was on disk."""

    def __init__(self, file_name):
        self._file_name = file_name
        self._mddf_sector_number: int = (
            -1
        )  # Populated below once the MDDF sector is found.
        self._mddf_sector_bytes: bytes = None
        self._fs_version: int = (
            0  # The file system version from the MDDF (14=LOS 1.0, 15=LOS 2.0, 16=LOS 3.0, 17=LOS 3.1).
        )
        self._is_dc42_format: bool = (
            None  # Either a DC42 formatted image file, or a raw disk image file.
        )
        self._num_sectors: int = -1
        self._single_tag_size: int = -1

        with open(self._file_name, "rb") as file:
            self._file_size = os.fstat(file.fileno()).st_size
            if self._file_size > 100000000:
                raise "File is longer than the maximum allowed 100,000,000 bytes! Exiting."
            if self._file_size < DC42_HEADER_SIZE:
                raise "File is shorter than the minimum allowed " + str(
                    DC42_HEADER_SIZE
                ) + " bytes! Exiting."
            self._file_bytes = file.read()  # Read the whole file
            if len(self._file_bytes) != self._file_size:
                raise "File size reported by the OS is different from what we could read! Exiting."
            # For convenience, we can access the in-memory file data through this binary file reader:
            self._file = BytesIO(self._file_bytes)

            # Read important fields
            self._file.seek(0x52)
            dc42_magic_number = struct.unpack(">H", self._file.read(2))[0]
            # Note: this is not 100% reliable, as there may be a raw profile image file with 0x0100 at offset 0x52.
            if dc42_magic_number != 0x0100:
                # A raw ProFile hard disk image file (not DC42). It is generally used only for ProFile hard disk images, not for floppy disk images.
                self._is_dc42_format = False
                self._num_sectors = self._file_size // (
                    512 + 20
                )  # 512 bytes of data + 20 bytes of tag data per sector
                self._single_tag_size = 20  # 20 bytes of tag data per sector
                print(
                    f"Found DC42 magic number = {dc42_magic_number:04X} in the DC42 header at offset 0x52, which most likely means that this is not a DC42 file. Assuming it is a raw disk image format."
                )

                print(f"File name: {file_name}")
                print(f"File size: {self._file_size}")
                print("File format: Raw ProFile or Widget hard disk image (not DC42)")
                print(f"Number of sectors: {self._num_sectors}")
                print(
                    f"Sector data size: {SECTOR_SIZE_IN_BYTES} (a hard-coded constant in the code)"
                )
                print(f"Sector tags size: {self._single_tag_size}")
                if self._file_size < (
                    self._num_sectors * (SECTOR_SIZE_IN_BYTES + self._single_tag_size)
                ):
                    print(
                        f"WARNING: Something is wrong: the file size {self._file_size} is less than the reported data+tag size {self._num_sectors*(SECTOR_SIZE_IN_BYTES + self._single_tag_size)} !!!"
                    )
                print(
                    f"Extra padding after header,data,tags: {self._file_size-(self._num_sectors*(SECTOR_SIZE_IN_BYTES + self._single_tag_size))} bytes."
                )
            else:
                self._is_dc42_format = True
                self._file.seek(0x40)
                data_size = struct.unpack(">I", self._file.read(4))[0]
                self._num_sectors = data_size // 512
                tag_data_size = struct.unpack(">I", self._file.read(4))[0]
                if tag_data_size == 0:
                    raise ValueError(
                        f"WARNING: the tags data size is 0, which means that there are no tags. Can not continue."
                    )
                self._single_tag_size = int(tag_data_size / self._num_sectors)

                # Read disk name (Pascal string, first byte is length)
                self._file.seek(0)
                disk_name = pascal_to_string(self._file.read(0x40 - 1))
                # Here is another way to get the data checksum on Linux: dd if=BLU090.dc42 bs=1 skip=72 count=4 status=none | xxd -p
                # Here is how to calculate the md5 on the data-only: hexdump -v -e '"%X"' -s 84 -n 871424 Xenix1of5.dc42 | md5   (the value 871424 is good only for Twiggy dc43 images)
                self._file.seek(0x48)
                data_checksum = struct.unpack(">I", self._file.read(4))[0]
                tag_checksum = struct.unpack(">I", self._file.read(4))[0]
                disk_type = struct.unpack("B", self._file.read(1))[0]
                disk_type_str = disk_type_to_string(disk_type)
                tag_size_str = tag_size_to_string(self._single_tag_size)
                disk_format = struct.unpack("B", self._file.read(1))[0]
                disk_format_str = disk_format_to_string(disk_format)
                magic_number = struct.unpack(">H", self._file.read(2))[0]

                # The data starts at offset HEADER_SIZE=0x54 (immediatelly after the header):
                computed_data_checksum = compute_dc42_checksum_of_file_data_block(
                    self._file,
                    DC42_HEADER_SIZE,
                    self._num_sectors * SECTOR_SIZE_IN_BYTES,
                )
                # The tags data is immediately after the data (starts at offset 0x54 + data_size)
                # For some reason, we need to skip the 1st tag (12 or 20 bytes), and compute the checksum of the rest:
                computed_tags_checksum = compute_dc42_checksum_of_file_data_block(
                    self._file,
                    DC42_HEADER_SIZE
                    + self._single_tag_size
                    + self._num_sectors * SECTOR_SIZE_IN_BYTES,
                    self._single_tag_size * (self._num_sectors - 1),
                )

                # Print extracted fields
                print(f"File name: {file_name}")
                print(f"File size: {self._file_size}")
                print("File format: DC42")
                print(f"Number of sectors: {self._num_sectors}")
                print(
                    f"Sector data size: {SECTOR_SIZE_IN_BYTES} (a hard-coded constant in the code)"
                )
                print(
                    f"Sector tags size: {self._single_tag_size} : {tag_size_str} : {"OK" if tag_size_str!="UNKNOWN!" else "BAD!"}"
                )
                if self._file_size < (
                    DC42_HEADER_SIZE
                    + self._num_sectors * SECTOR_SIZE_IN_BYTES
                    + self._num_sectors * self._single_tag_size
                ):
                    print(
                        f"WARNING: Something is wrong: the file size {self._file_size} is less than the reported header size {DC42_HEADER_SIZE} + data size {self._num_sectors* SECTOR_SIZE_IN_BYTES} + tag size {self._num_sectors*self._single_tag_size}  which totals to {DC42_HEADER_SIZE + self._num_sectors*SECTOR_SIZE_IN_BYTES + self._num_sectors*self._single_tag_size} bytes"
                    )
                print(
                    f"DC42 Header Size: {DC42_HEADER_SIZE:2} (a hard-coded constant in the code)"
                )
                print(f"Disk Name in DC42 header: {disk_name}")
                print(
                    f"Data Size: {self._num_sectors* SECTOR_SIZE_IN_BYTES:7} bytes = {self._num_sectors:4} sectors of 512 bytes each {"OK" if self._num_sectors in({800,1600,1702,9728,19456}) else "UNUSUAL!"}"
                )
                print(
                    f"Tag Size:  {(self._single_tag_size*self._num_sectors):7} bytes = {self._num_sectors:4} sectors of  {self._single_tag_size} bytes each {"OK" if self._num_sectors in({0, 800,1600,1702,9728,19456}) else "UNUSUAL!"}"
                )
                print(
                    f"Extra padding after header,data,tags: {self._file_size-(DC42_HEADER_SIZE + self._num_sectors* SECTOR_SIZE_IN_BYTES + self._single_tag_size*self._num_sectors)} bytes."
                )
                print(f"Data Checksum in header: {data_checksum:#010x}")
                print(
                    f"Computed Data Checksum:  {computed_data_checksum:#010x} : {"OK" if data_checksum==computed_data_checksum else "BAD!"}"
                )
                print(f"Tag Checksum in header:  {tag_checksum:#010x}")
                print(
                    f"Computed Tag Checksum:   {computed_tags_checksum:#010x} : {"OK" if tag_checksum==computed_tags_checksum else "BAD!"}"
                )
                print(
                    f"Disk Type:   {disk_type:#04x} = {disk_type_str:10} : {"OK" if disk_type_str!="UNKNOWN!" else "BAD!"}"
                )
                print(
                    f"Format:      {disk_format:#04x} = {disk_format_str:10} : {"OK" if disk_format_str!="UNKNOWN!" else "BAD!"}"
                )
                print(
                    f"DC42 Magic number:{magic_number:#06x}            : {"OK" if magic_number==0x0100 else "BAD!"}"
                )

            # Derive the MDDF (Media Descriptor Data File) sector number from the boot sector (sector 0),
            # exactly like the Lisa boot ROM and boot loader do.
            self._mddf_sector_number = derive_mddf_sector_number_from_boot_sector_zero(
                self.read_sector(0), self._num_sectors
            )
            # Sanity check: the derived sector must carry the MDDF tag (file_id 0x0001).
            if not (
                0 <= self._mddf_sector_number < self._num_sectors
                and self.get_file_id_from_tag_data_for_sector(self._mddf_sector_number)
                == 0x0001
            ):
                # Last resort: scan the tags of all sectors for file_id 0x0001 (the old way).
                print(
                    "WARNING: could not derive the MDDF sector number from the boot sector; falling back to scanning all tags for file_id 0x0001."
                )
                mddf_sector_numbers = (
                    self.find_all_sector_numbers_with_given_file_id_type(0x0001)
                )
                if len(mddf_sector_numbers) == 0:
                    raise ValueError(
                        "Could not find the MDDF (Media Descriptor Data File) sector (aka file_id 0x0001). Cannot continue, exiting!"
                    )
                self._mddf_sector_number = mddf_sector_numbers[0]
            if self._mddf_sector_number == -1:
                raise ValueError(
                    "Could not find the MDDF (Media Descriptor Data File) sector. Cannot continue, exiting!"
                )

            self._mddf_sector_bytes = self.read_sector(self._mddf_sector_number)

            self._fs_version = to_uint16_big_endian(self._mddf_sector_bytes, 0)
            print(
                f"\nFile system version (found in in MDDF sector {self._mddf_sector_number}) is {self._fs_version} (aka {file_system_version_to_string(self._fs_version)})"
            )
            if self._fs_version < 14 or self._fs_version > 17:
                raise ValueError(
                    f"The file system version {self._fs_version} is outside of the known range 14..17."
                    " This program does not work with this version. Cannot continue, exiting!"
                )

    def _is_disk_image_file_currently_open_by_some_other_process(self) -> bool:
        """
        Check if the disk image file (self._file_name) is currently open by any
        running process.

        Works on Linux, macOS, and Windows, but note:
        - Requires elevated privileges (root/admin) to see files opened
            by processes you don't own; otherwise those processes are skipped.
        - On Windows, checking open files can occasionally be slow for
            certain processes.

        Returns:
            True if the file is open by at least one process, False otherwise.

        """
        target_path = os.path.abspath(self._file_name)
        for proc in psutil.process_iter(['pid', 'name']):
            try:
                for f in proc.open_files():
                    if os.path.abspath(f.path) == target_path:
                        return True
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            except Exception:
                # Defensive catch-all for platform-specific quirks
                # (e.g., transient WinError access issues)
                continue
        return False

    def _confirm_proceed_if_disk_image_is_open_by_other_process(self) -> bool:
        """
        Check if the disk image file is currently open by some other process. If it
        is, warn the user (a concurrent process may hold stale copies of the image
        in memory or write to it as well, so modifying it now is risky) and ask
        whether to continue anyway.

        Returns:
            True to proceed with the operation, False to abort it.
        """
        if not self._is_disk_image_file_currently_open_by_some_other_process():
            return True
        print(
            f"\nWARNING: The disk image file '{self._file_name}' appears to be currently open"
            " by some other process. Modifying it now may conflict with that process."
        )
        answer = input("Do you want to continue anyway? [y/N] ")
        return answer.strip().lower() in ("y", "yes")

    def print_extra_info(self):
        self.print_mddf_sector_info()

        # allocation_bitmap_sector_numbers = self.find_all_sector_numbers_with_given_file_id_type(0x0002)
        # self.print_allocation_bitmap_sector_info(self._file, allocation_bitmap_sector_numbers)

        # self.print_sector_tags(0, 20)

        # Note: the "hint sectors" don't have a fixed file id. Sample hint sector ids found in the wild: 0xFFBD, 0xFFE4, 0xFFF9.
        # It seems that they all start with 0xFF, but I don't know if that is always the case; so we can't find them by file id.
        # There are too-many free sectors: print(f"List of sector numbers with file id FILEID_FREE    (0x0000) : {self.find_all_sector_numbers_with_given_file_id_type(0x0000)}")
        print(f"Printing some predefined sector types, derived from their tag data:")
        print(
            f"List of sector numbers with file id FILEID_MDDF    (0x0001) : {self.find_all_sector_numbers_with_given_file_id_type(0x0001)}"
        )
        print(
            f"List of sector numbers with file id FILEID_BITMAP  (0x0002) : {self.find_all_sector_numbers_with_given_file_id_type(0x0002)}"
        )
        print(
            f"List of sector numbers with file id FILEID_SRECORD (0x0003) : {self.find_all_sector_numbers_with_given_file_id_type(0x0003)}"
        )
        print(
            f"List of sector numbers with file id FILEID_CATALOG (0x0004) : {self.find_all_sector_numbers_with_given_file_id_type(0x0004)}"
        )
        print(
            f"List of sector numbers with file id FILEID_ERASED  (0x7FFF) : {self.find_all_sector_numbers_with_given_file_id_type(0x7FFF)}"
        )
        print(
            f"List of sector numbers with file id FILEID_BOOT    (0xAAAA) : {self.find_all_sector_numbers_with_given_file_id_type(0xAAAA)}"
        )
        print(
            f"List of sector numbers with file id FILEID_LOADER  (0xBBBB) : {self.find_all_sector_numbers_with_given_file_id_type(0xBBBB)}"
        )

        # self.dump_bitmap_sectors()

        # self.dump_free_bitmap_sectors_numbers()

        # Print whether a range of sectors are marked as free in the allocation bitmap:
        # mddf_sector_bytes = self._mddf_sector_bytes
        # absolute_bitmap_start_sector_number = to_uint32_big_endian(mddf_sector_bytes, 136) + self._mddf_sector_number
        # num_bitmap_sectors = to_uint16_big_endian(mddf_sector_bytes, 146)
        # for absolute_sector_number in range(9000, 9200):
        #     is_butmap_sector_free = self.is_sector_free_in_bitmap(absolute_bitmap_start_sector_number, num_bitmap_sectors, absolute_sector_number)
        #     print(f"\n###################################################### Checking if sector {absolute_sector_number} is marked as free in the allocation bitmap: {'YES' if is_butmap_sector_free else 'NO'}")

        # if self.is_flat_catalog_volume():
        #     # fs_version 14/15 (LOS 1.0 / LOS 2.0) volumes have no B-tree catalog:
        #     self.flat_catalog_list_files()
        #     self.flat_catalog_dump_catalog()
        # else:
        #     self.dump_catalog()

        # print("\nDumping tags for a few sectors ...")
        # self.pretty_print_tags_for_sector(270)

        # self.check_bitmap_for_all_file_data()

        # first_sector = self.read_sector(0)
        # print_bytes_in_hex_and_ascii(first_sector)

        # print_bytes_in_hex_and_ascii(self._mddf_sector_bytes)

        # The hint sector for file "{T4}obj" (ak Lisadraw) in file lisaem-profile.dc42 :
        # self.print_hint_sector_info(172)
        # self.pretty_print_tags_for_sector(172)

    def remove_file_protection(self):
        """
        Find all the protected files on this disk image (see find_protected_files()), then
        edit each file's hint (hentry) sector on disk, setting:
          * machine_id (4 bytes, at offset 0x42 in the hint sector) to 0x00000000, and
          * the 'protected' flag (1 byte, at offset 0x48 in the hint sector) to 0.
        With both fields cleared, the theft-protection check in GOPEN passes on any
        machine, so the files can be opened anywhere (see print_hint_sector_info()).

        self._file (an in-memory BytesIO copy of the image) is kept in sync with every
        write so that the new per-sector tag checksum and the DC42 header checksums can
        be computed from it. self._file_name is opened for binary writing and just the
        5 bytes that need to change (plus the tag checksum byte) are overwritten in each
        affected hint sector.
        """
        protected_file_hint_sector_numbers: list[int] = self.find_protected_files()
        if not protected_file_hint_sector_numbers:
            print("No protected files found; nothing to do. Exiting.")
            return

        answer = input(
            f"\nNow will remove the disk protection for {len(protected_file_hint_sector_numbers)} file(s). This will modify the disk image file '{self._file_name}'. Trust me? [y/N] "
        )
        if answer.strip().lower() not in ("y", "yes"):
            print("Aborted by user; the disk image was not modified.")
            return

        print(
            f"Removing the protection from {len(protected_file_hint_sector_numbers)} file(s) in '{self._file_name}' ..."
        )
        try:
            file_in_rw_mode = open(self._file_name, "r+b")
        except OSError as e:
            print(f"ERROR: cannot open '{self._file_name}' for writing: {e}")
            return
        with file_in_rw_mode:
            for hint_sector_number in protected_file_hint_sector_numbers:
                if self._is_dc42_format:
                    sector_start_file_offset: int = (
                        DC42_HEADER_SIZE + hint_sector_number * SECTOR_SIZE_IN_BYTES
                    )
                else:
                    # The raw image file format is: a set of sectors, where each sector has 20-bytes tag data followed by 512 bytes sector data.
                    sector_start_file_offset: int = (
                        interleave5(hint_sector_number)
                        * (SECTOR_SIZE_IN_BYTES + self._single_tag_size)
                        + self._single_tag_size
                    )

                machine_id_offset = (
                    sector_start_file_offset + 0x42
                )  # machine_id: 4 bytes in the hentry (see print_hint_sector_info())
                protected_offset = (
                    sector_start_file_offset + 0x48
                )  # 'protected' flag: 1 byte in the hentry

                # We write the same data to the file on disk and to the in-memory copy self._file, so that we can later compute the new tag checksum on the new in-memory data.
                file_in_rw_mode.seek(machine_id_offset)
                file_in_rw_mode.write(b"\x00\x00\x00\x00")
                self._file.seek(machine_id_offset)
                self._file.write(b"\x00\x00\x00\x00")

                file_in_rw_mode.seek(protected_offset)
                file_in_rw_mode.write(b"\x00")
                self._file.seek(protected_offset)
                self._file.write(b"\x00")

                print(
                    f"  Hint sector {hint_sector_number}: wrote machine_id=0x00000000 at file offset {machine_id_offset:#08x} "
                    f"and protected=0 at file offset {protected_offset:#08x}"
                )
                # Read the sector back from disk (self._file is stale) to verify the change:
                file_in_rw_mode.seek(machine_id_offset)
                machine_id_readback = file_in_rw_mode.read(4)
                file_in_rw_mode.seek(protected_offset)
                protected_readback = file_in_rw_mode.read(1)[0]
                if (
                    machine_id_readback == b"\x00\x00\x00\x00"
                    and protected_readback == 0
                ):
                    print(
                        f"  Verified hint sector {hint_sector_number}: machine_id=0x00000000, protected=0 -> OK"
                    )
                else:
                    print(
                        f"  WARNING: verification FAILED for hint sector {hint_sector_number}: machine_id={int.from_bytes(machine_id_readback,'big'):#010x}, protected={protected_readback}!"
                    )

                # The hentry bytes written above change the sector data, so the per-sector tag
                # checksum must be updated too -- but ONLY for 20-byte tags (hard-disk images):
                # 12-byte floppy tags have NO checksum field (byte 11 is the low byte of the
                # backward link, see calculate_new_tag_checksum()).
                # For completeness, we update the tag checksum both in the file on disk and in the in-memory copy self._file, even though the latter is not strictly necessary.
                if self._single_tag_size == 20:
                    new_tag_checksum = self.calculate_new_tag_checksum(
                        hint_sector_number
                    )
                    if self._is_dc42_format:
                        tag_checksum_offset = (
                            DC42_HEADER_SIZE
                            + self._num_sectors * SECTOR_SIZE_IN_BYTES
                            + hint_sector_number * self._single_tag_size
                            + 11
                        )
                    else:
                        # Raw image format: the tag immediately precedes its sector's data.
                        tag_checksum_offset = (
                            sector_start_file_offset - self._single_tag_size + 11
                        )
                    file_in_rw_mode.seek(tag_checksum_offset)
                    file_in_rw_mode.write(bytes([new_tag_checksum]))
                    self._file.seek(tag_checksum_offset)
                    self._file.write(bytes([new_tag_checksum]))
                    file_in_rw_mode.seek(tag_checksum_offset)
                    tag_checksum_readback = file_in_rw_mode.read(1)[0]
                    if tag_checksum_readback == new_tag_checksum:
                        print(
                            f"  Updated tag checksum byte 11 to {new_tag_checksum:#04x} at file offset {tag_checksum_offset:#08x} -> OK"
                        )
                    else:
                        print(
                            f"  WARNING: tag checksum read-back test failed: {tag_checksum_readback:#04x} != expected {new_tag_checksum:#04x} for hint sector {hint_sector_number}!"
                        )
                else:
                    print(
                        f"  (12-byte floppy tags have no per-sector checksum; no tag update needed.)"
                    )

            # Update the Data and Tags DC42 checksum values in the DC42 header (if this is a DC42 diks image file):
            # they are now wrong because we changed the sector data.
            # (No confirmation prompt: the user already confirmed above.)
            self.fix_dc42_checksum(confirm=False)

    def fix_invalid_sector_checksums(self):
        """Scan ALL sectors of the image and fix any invalid per-sector tag checksums.

        For every sector whose XOR of all 20 tag bytes (including byte 11) plus all 512
        data bytes is not 0, byte 11 is recomputed from the current on-disk bytes and
        rewritten. (This never touches the sector data - only the checksum byte in the tags,
        so it is safe on any image. 12-byte floppy tags have no checksum byte and are skipped.)
        """
        if self._single_tag_size != 20:
            print(
                "12-byte floppy tags have no per-sector checksum byte; nothing to do."
            )
            return
        print("Scanning all sectors for invalid per-sector tag checksums ...")
        bad_sectors: list[int] = []
        for sector_number in range(self._num_sectors):
            if self._is_dc42_format:
                data_pos = DC42_HEADER_SIZE + sector_number * SECTOR_SIZE_IN_BYTES
                tag_pos = (
                    DC42_HEADER_SIZE
                    + self._num_sectors * SECTOR_SIZE_IN_BYTES
                    + sector_number * self._single_tag_size
                )
            else:
                tag_pos = interleave5(sector_number) * (
                    SECTOR_SIZE_IN_BYTES + self._single_tag_size
                )
                data_pos = tag_pos + self._single_tag_size
            self._file.seek(data_pos)
            data_bytes = self._file.read(SECTOR_SIZE_IN_BYTES)
            self._file.seek(tag_pos)
            tag_bytes = self._file.read(self._single_tag_size)
            xor_all = 0
            for b in data_bytes:
                xor_all ^= b
            for b in tag_bytes:
                xor_all ^= b
            if xor_all != 0:
                bad_sectors.append(sector_number)
        if not bad_sectors:
            print(
                f"All {self._num_sectors} sectors have valid tag checksums; nothing to do."
            )
            return
        print(
            f"Found {len(bad_sectors)} sector(s) with an invalid tag checksum: {bad_sectors}"
        )
        for sector_number in bad_sectors:
            if self._is_dc42_format:
                data_pos = DC42_HEADER_SIZE + sector_number * SECTOR_SIZE_IN_BYTES
                tag_pos = (
                    DC42_HEADER_SIZE
                    + self._num_sectors * SECTOR_SIZE_IN_BYTES
                    + sector_number * self._single_tag_size
                )
            else:
                tag_pos = interleave5(sector_number) * (
                    SECTOR_SIZE_IN_BYTES + self._single_tag_size
                )
                data_pos = tag_pos + self._single_tag_size
            self._file.seek(data_pos)
            data_bytes = self._file.read(SECTOR_SIZE_IN_BYTES)
            self._file.seek(tag_pos)
            tag_bytes = bytearray(self._file.read(self._single_tag_size))
            new_ck = 0
            for i in range(len(tag_bytes)):
                if i != 11:
                    new_ck ^= tag_bytes[i]
            for b in data_bytes:
                new_ck ^= b
            print(
                f"  Sector {sector_number}: stored byte 11 = {tag_bytes[11]:#04x}, correct byte 11 = {new_ck:#04x}"
            )
        answer = input(
            f"\nFix these {len(bad_sectors)} sector checksum byte(s) in '{self._file_name}'? [y/N] "
        )
        if answer.strip().lower() not in ("y", "yes"):
            print("Aborted by user; the disk image was not modified.")
            return
        try:
            file_in_rw_mode = open(self._file_name, "r+b")
        except OSError as e:
            print(f"ERROR: cannot open '{self._file_name}' for writing: {e}")
            return
        with file_in_rw_mode:
            for sector_number in bad_sectors:
                if self._is_dc42_format:
                    data_pos = DC42_HEADER_SIZE + sector_number * SECTOR_SIZE_IN_BYTES
                    tag_pos = (
                        DC42_HEADER_SIZE
                        + self._num_sectors * SECTOR_SIZE_IN_BYTES
                        + sector_number * self._single_tag_size
                    )
                else:
                    tag_pos = interleave5(sector_number) * (
                        SECTOR_SIZE_IN_BYTES + self._single_tag_size
                    )
                    data_pos = tag_pos + self._single_tag_size
                file_in_rw_mode.seek(data_pos)
                data_bytes = file_in_rw_mode.read(SECTOR_SIZE_IN_BYTES)
                file_in_rw_mode.seek(tag_pos)
                tag_bytes = bytearray(file_in_rw_mode.read(self._single_tag_size))
                new_ck = 0
                for i in range(len(tag_bytes)):
                    if i != 11:
                        new_ck ^= tag_bytes[i]
                for b in data_bytes:
                    new_ck ^= b
                tag_bytes[11] = new_ck
                file_in_rw_mode.seek(tag_pos)
                file_in_rw_mode.write(bytes(tag_bytes))
                self._file.seek(tag_pos)
                self._file.write(bytes(tag_bytes))
                # Verify from disk:
                file_in_rw_mode.seek(data_pos)
                d2 = file_in_rw_mode.read(SECTOR_SIZE_IN_BYTES)
                file_in_rw_mode.seek(tag_pos)
                t2 = file_in_rw_mode.read(self._single_tag_size)
                x = 0
                for b in d2:
                    x ^= b
                for b in t2:
                    x ^= b
                if x == 0:
                    print(
                        f"  Sector {sector_number}: fixed byte 11 -> {new_ck:#04x}, verified OK."
                    )
                else:
                    print(
                        f"  ERROR: sector {sector_number} still invalid after fix (XOR = {x:#04x})!"
                    )
        print("Done.")

    def fix_dc42_checksum(self, confirm: bool = True):
        """Check the data and tags checksums in the DC42 header, and fix them only if they are wrong.

        The DC42 header stores a 32-bit checksum of the sector data at offset 0x48 and a
        32-bit checksum of the sector tags (excluding the first tag) at offset 0x4C. Both
        checksums are recomputed from the current image contents (self._file); each stored
        value that does not match its computed value is rewritten in the file on disk (and
        in the in-memory copy self._file). If both stored checksums are already correct,
        nothing is written. Raw (non-DC42) images have no such header checksums, so for
        them there is nothing to do.
        """
        if not self._is_dc42_format:
            print(
                "Not a DC42 file: there are no DC42 header checksums to fix; nothing to do."
            )
            return

        self._file.seek(0x48)
        stored_data_checksum = struct.unpack(">I", self._file.read(4))[0]
        self._file.seek(0x4C)
        stored_tags_checksum = struct.unpack(">I", self._file.read(4))[0]

        new_computed_dc42_data_checksum = compute_dc42_checksum_of_file_data_block(
            self._file, DC42_HEADER_SIZE, self._num_sectors * SECTOR_SIZE_IN_BYTES
        )
        new_computed_dc42_tags_checksum = compute_dc42_checksum_of_file_data_block(
            self._file,
            DC42_HEADER_SIZE
            + self._single_tag_size
            + self._num_sectors * SECTOR_SIZE_IN_BYTES,
            self._num_sectors * self._single_tag_size - self._single_tag_size,
        )

        data_checksum_is_wrong = stored_data_checksum != new_computed_dc42_data_checksum
        tags_checksum_is_wrong = stored_tags_checksum != new_computed_dc42_tags_checksum
        if not data_checksum_is_wrong and not tags_checksum_is_wrong:
            print(
                f"DC42 header checksums are already correct (data = {stored_data_checksum:#010x}, tags = {stored_tags_checksum:#010x}); nothing to do."
            )
            return

        if data_checksum_is_wrong:
            print(
                f"Data checksum in DC42 header is {stored_data_checksum:#010x} but the computed value is {new_computed_dc42_data_checksum:#010x} - WRONG."
            )
        if tags_checksum_is_wrong:
            print(
                f"Tags checksum in DC42 header is {stored_tags_checksum:#010x} but the computed value is {new_computed_dc42_tags_checksum:#010x} - WRONG."
            )

        if confirm:
            answer = input(
                f"\nFix the wrong DC42 header checksum(s) in '{self._file_name}'? [y/N] "
            )
            if answer.strip().lower() not in ("y", "yes"):
                print("Aborted by user; the disk image was not modified.")
                return

        try:
            file_in_rw_mode = open(self._file_name, "r+b")
        except OSError as e:
            print(f"ERROR: cannot open '{self._file_name}' for writing: {e}")
            return
        with file_in_rw_mode:
            # For completeness, we update the DC42 checksums both in the file on disk and in the in-memory copy self._file, even though the latter is not strictly necessary.
            if data_checksum_is_wrong:
                file_in_rw_mode.seek(0x48)
                file_in_rw_mode.write(
                    new_computed_dc42_data_checksum.to_bytes(
                        4, byteorder="big", signed=False
                    )
                )
                self._file.seek(0x48)
                self._file.write(
                    new_computed_dc42_data_checksum.to_bytes(
                        4, byteorder="big", signed=False
                    )
                )
                print(
                    f"Updated DC42 header data checksum = {new_computed_dc42_data_checksum:#010x}"
                )
            if tags_checksum_is_wrong:
                file_in_rw_mode.seek(0x4C)
                file_in_rw_mode.write(
                    new_computed_dc42_tags_checksum.to_bytes(
                        4, byteorder="big", signed=False
                    )
                )
                self._file.seek(0x4C)
                self._file.write(
                    new_computed_dc42_tags_checksum.to_bytes(
                        4, byteorder="big", signed=False
                    )
                )
                print(
                    f"Updated DC42 header tags checksum = {new_computed_dc42_tags_checksum:#010x}"
                )
        print("Done.")

    def calculate_new_tag_checksum(self, sector_number: int) -> int:
        """
        Calculates the per-sector tag checksum byte for a given sector number.

        ONLY VALID FOR 20-BYTE TAG DATA (HARD DISK IMAGES). In a 20-byte tag the
        checksum is stored in byte 11 of the tag, so byte 11 is excluded from the
        calculation: checksum = XOR of all 512 bytes of the sector data, XOR'd with
        all tag bytes except byte 11. Verified empirically against a real hard disk
        image (lisaem-profile.dc42): 9728/9728 sectors matched.

        12-BYTE TAGS (FLOPPY IMAGES) HAVE NO CHECKSUM FIELD AT ALL. Their layout
        (per FINISH_READ in Lisa source file SOURCE-SONYASM.TEXT.unix.txt, "UNPACK THE
        12 BYTE HEADER") is:
            version(2) vol_id(2) file_id(2) rel_num(2)
            fwd_link(11 bits) + dataused(5 bits)    at bytes 8-9
            bkwd_link(11 bits) + dataused(11 bits)  at bytes 10-11
        where the links are MDDF-relative sector numbers and 0x07FF is the "no link"
        sentinel (the driver sign-extends it to 0xFFFFFFFF). Byte 11 is therefore the
        low byte of the backward link, NOT a checksum. This layout was verified
        against every file-data sector of a real 400K floppy image (has_one_file.dc42):
        all fwd/bkwd links and rel_num values reconstructed correctly.

        The result of XOR operations on 8-bit values will always stay within 0-255.
        """
        if self._single_tag_size != 20:
            raise ValueError(
                f"Per-sector tag checksums only exist in 20-byte tag data (hard disk images). "
                f"This image has {self._single_tag_size}-byte tags (floppy format), which contain no checksum byte "
                f"(byte 11 is the low byte of the backward link). See the docstring of this function."
            )
        checksum_byte = 0x00  # Initialize checksum to 0

        # Calculate checksum from sector data
        sector_bytes = self.read_sector(sector_number)
        # In Python, data[i] directly gives the integer value (0-255) of the byte,
        # so no '& 0xFF' is needed like in C.
        for i in range(0, len(sector_bytes)):
            checksum_byte = checksum_byte ^ sector_bytes[i]

        # Calculate checksum from tag data, excluding byte at index 11
        tag_bytes = self.read_tags_for_sector(sector_number)
        for i in range(0, len(tag_bytes)):
            if i != 11:  # Exclude the byte at index 11 from checksum calculation
                checksum_byte = checksum_byte ^ tag_bytes[i]

        if checksum_byte < 0 or checksum_byte > 255:
            raise ValueError(
                f"The checksum calculation produced a value of {checksum_byte}, which is outside of the allowed 0..255 ! Exiting."
            )
        return checksum_byte

    def print_hint_sector_info(self, hint_sector_number: int):
        """Prints the hentry record (file header) found at the start of the given hint sector.

        Each file has a set of "hint" sectors; the first one starts with the hentry record,
        defined in Lisa source file LISA_OS/OS/source-fsprim.text.unix.txt:

            hentry = record
                name        : e_name;         (* Pascal string: in fs_version 14/15 e_name = string[33]
                                                = 34 bytes; in fs_version 16/17 e_name = string[32] = 33 bytes
                                                plus 1 pad byte. Either way the name field occupies
                                                offsets 0x00..0x21. *)
                unique_ID   : UID;            (* two longints (a, b), 8 bytes total *)
                version     : integer;        (* file format version, 21 = cur_file_version *)
                ftype       : filetype;       (* enum, e.g. 14 = userfile *)
                DTC, DTA, DTM, DTB, DTS : longint;  (* dates, seconds since 1901-01-01, in GMT *)
                machine_id  : longint;        (* machine this file may be opened on (theft protection) *)
                killed, safety_on, protected, master, scavenged, closed_by_OS, file_open : boolean;
                result_scavenge, unusedi1, system_type, user_type, user_subtype : integer;
                build_info  : Build_Control;  (* 4 x integer *)
                file_portion: integer;
                password    : Str8;           (* Pascal string, 1 length byte + 8 bytes *)
                parentID    : NodeIdent;      (* longint *)
                fsOverhead  : integer;
            end;

        Note that the fields are NOT tightly packed: there is a 1-byte gap after the name field,
        so unique_ID starts at offset 0x22 (not 0x21). This was verified against real hint sector
        data: at 0x2A the version field reads 21 (cur_file_version), at 0x2C the ftype reads
        14 (userfile), and the five date fields at 0x2E..0x41 are valid Lisa dates.

        After the hentry (which ends at 0x6F), at offset 0x80 there is the small file map:
        a longint "size" (number of blocks), followed by (address: longint, cpages: integer) entries.

        The theft-protection check (GOPEN in LISA_OS/OS/source-fsui1.text.unix.txt) allows opening
        the file only if: master, or not protected, or machine_id equals the machine's serial
        number (see LISA_OS/OS/source-SERNUM.TEXT.unix.txt). Otherwise the user gets error 142:
        "<file_name> is a protected file".
        """
        sector_bytes = self.read_sector(hint_sector_number)

        file_id = self.get_file_id_from_tag_data_for_sector(hint_sector_number)
        print(
            f"\nHentry info from hint sector {hint_sector_number} (file_id {self.get_file_id_type_from_tag_data_for_sector(hint_sector_number)} ({file_id:#06x})) :"
        )

        # 0x00: name (e_name, Pascal string: 1 length byte + name bytes)
        file_name = pascal_to_string(sector_bytes, start=0)
        print(f"  At 0x00 : {'name':>18}: '{file_name}'")

        # 0x22..0x29: unique_ID (UID = two longints a, b)
        uid_a = to_uint32_big_endian(sector_bytes, 0x22)
        uid_b = to_uint32_big_endian(sector_bytes, 0x26)
        print(f"  At 0x22 : {'unique_ID':>18}: a={uid_a:#010x} b={uid_b:#010x}")

        # 0x2A: version (21 = cur_file_version, see LISA_OS/OS/source-sfileio.text.unix.txt)
        version = to_uint16_big_endian(sector_bytes, 0x2A)
        print(
            f"  At 0x2A : {'version':>18}: {version} ({version:#06x})"
            + ("  # 21 = cur_file_version" if version == 21 else "")
        )

        # 0x2C: ftype (filetype enum, see LISA_OS/OS/source-sfileio.text.unix.txt)
        # ftype occupies 1 byte at 0x2C in all versions (verified on fs_version 15 and 17
        # images: user files store 0x0e there, followed by a 0x00 pad byte at 0x2D; a
        # big-endian u16 read of those bytes would give the bogus value 0x0e00).
        # In fs_version 14/15 the byte at 0x2D is uninitialized (the hentry is only 88 bytes,
        # and these system files' hentries were written without ClearMem), so it can be junk.
        ftype = sector_bytes[0x2C]
        print(
            f"  At 0x2C : {'ftype':>18}: {ftype} ({ftype:#04x}) = {FILETYPE_NAMES.get(ftype, 'UNKNOWN!')}"
        )
        if self.is_flat_catalog_volume():
            print(
                f"  At 0x2D : {'unknown byte':>18}: {sector_bytes[0x2D]} ({sector_bytes[0x2D]:#04x})"
            )

        # 0x2E..0x41: the five date fields (seconds since 1901-01-01, in GMT, like the MDDF DT_ fields).
        for date_offset, date_name in (
            (0x2E, "DTC (created)"),
            (0x32, "DTA (accessed)"),
            (0x36, "DTM (modified)"),
            (0x3A, "DTB (backup)"),
            (0x3E, "DTS (scavenged)"),
        ):
            date_value = to_uint32_big_endian(sector_bytes, date_offset)
            if date_value == 0:
                date_str = "0 (never)"
            else:
                date_str = f"{date_value:#010x} (date '{format_date(date_value)}')"
            print(f"  At {date_offset:#04x} : {date_name:>18}: {date_str}")

        # 0x42: machine_id (the machine this file may be opened on; used for theft protection)
        machine_id = to_uint32_big_endian(sector_bytes, 0x42)
        machine_id_extra = ""
        if machine_id != 0:
            # Per LISA_OS/OS/source-SERNUM.TEXT.unix.txt: machine_id = first3 * 65536 + last5, where
            # first3/last5 are the BCD digits of the 8-digit AppleNet serial number.
            machine_id_extra = f"  # AppleNet digits {machine_id // 65536:03d}-{machine_id % 65536:05d}"
        print(
            f"  At 0x42 : {'machine_id':>18}: {machine_id} ({machine_id:#010x}){machine_id_extra}"
        )

        # 0x46..0x4C: the seven 1-byte boolean flags
        for bool_name, bool_offset in (
            ("killed", 0x46),
            ("safety_on", 0x47),
            ("protected", 0x48),
            ("master", 0x49),
            ("scavenged", 0x4A),
            ("closed_by_OS", 0x4B),
            ("file_open", 0x4C),
        ):
            print(
                f"  At {bool_offset:#04x} : {bool_name:>18}: {sector_bytes[bool_offset]}"
            )

        # 0x4D..0x56: the 2-byte integer fields
        for int_name, int_offset in (
            ("result_scavenge", 0x4D),
            ("unusedi1", 0x4F),
            ("system_type", 0x51),
            ("user_type", 0x53),
            ("user_subtype", 0x55),
        ):
            int_value = to_uint16_big_endian(sector_bytes, int_offset)
            print(
                f"  At {int_offset:#04x} : {int_name:>18}: {int_value} ({int_value:#06x})"
            )

        if self.is_flat_catalog_volume():
            # fs_version 14/15 (flat catalog) hentries are only 88 bytes long: the fields below
            # (build_info .. fsOverhead) don't exist in that version (see UpgradeFile in
            # LISA_OS/OS/source-fsprim.text.unix.txt, which zeroes the 40 bytes after the
            # 88-byte hentry when upgrading such volumes).
            print(
                f"  At 0x56 : {'(hentry ends)':>18}: the hentry is 88 bytes in fs_version {self._fs_version}; "
                f"build_info/file_portion/password/parentID/fsOverhead do not exist."
            )
        else:
            # 0x57..0x5E: build_info (Build_Control = 4 x integer)
            print(
                f"  At 0x57 : {'build_info':>18}: release={to_uint16_big_endian(sector_bytes, 0x57)} "
                f"build={to_uint16_big_endian(sector_bytes, 0x59)} "
                f"compat={to_uint16_big_endian(sector_bytes, 0x5B)} "
                f"revision={to_uint16_big_endian(sector_bytes, 0x5D)}"
            )

            # 0x5F: file_portion
            file_portion = to_uint16_big_endian(sector_bytes, 0x5F)
            print(
                f"  At 0x5F : {'file_portion':>18}: {file_portion} ({file_portion:#06x})"
            )

            # 0x61: password (Str8, Pascal string)
            password = pascal_to_string(sector_bytes, start=0x61)
            print(f"  At 0x61 : {'password':>18}: '{password}'")

            # 0x6A: parentID
            parent_id = to_uint32_big_endian(sector_bytes, 0x6A)
            print(f"  At 0x6A : {'parentID':>18}: {parent_id} ({parent_id:#010x})")

            # 0x6E: fsOverhead
            fs_overhead = to_uint16_big_endian(sector_bytes, 0x6E)
            print(f"  At 0x6E : {'fsOverhead':>18}: {fs_overhead} ({fs_overhead:#06x})")

        # Theft-protection summary: GOPEN in LISA_OS/OS/source-fsui1.text.unix.txt compares machine_id to the
        # machine's serial number; on mismatch the open fails with error 142: "<file_name> is a protected file":
        is_protected = bool(sector_bytes[0x48])
        is_master = bool(sector_bytes[0x49])
        if is_protected and not is_master:
            print(
                f"  >>> This file IS theft-protected: it can only be opened on the machine whose serial number "
                f"decodes to machine_id {machine_id} ({machine_id:#010x}). On any other machine, opening it fails "
                f"with error 142: '{file_name} is a protected file'"
            )
        elif is_protected and is_master:
            print(
                f"  >>> This file is a protected MASTER: it can be opened on any machine."
            )
        else:
            print(
                f"  >>> This file is NOT theft-protected: it can be opened on any machine."
            )

        # 0x80: the small file map ("smallmap" in the Lisa source):
        #     size : longint;       (* @0x80, number of PAGES in the file; GET_PSIZE = size * pgdatasize *)
        #     max_entries : integer;(* @0x84, usually 9 *)
        #     ecount  : integer;    (* @0x86, number of entries currently in use *)
        #     map[0..9] : mapentry; (* @0x88, 10 x (address: longint; cpages: integer) = 6 bytes each *)
        # The addresses are MDDF-relative sector numbers. Note that the smallmap is a display
        # optimization only: to actually read the file data, follow the tag fwd_links from the
        # slist's fileaddr. (Verified against both fs 15 and fs 17 volumes; a stale smallmap can
        # occur after a file is shrunk.)
        file_map_size = to_uint32_big_endian(sector_bytes, 0x80)
        file_map_max_entries = to_uint16_big_endian(sector_bytes, 0x84)
        file_map_ecount = to_uint16_big_endian(sector_bytes, 0x86)
        file_map_entries = []
        for entry_index in range(0, min(file_map_ecount, 10)):
            entry_offset = 0x88 + entry_index * 6
            address = to_uint32_big_endian(sector_bytes, entry_offset)
            cpages = to_uint16_big_endian(sector_bytes, entry_offset + 4)
            file_map_entries.append((address, cpages))
        print(
            f"  At 0x80 : {'file map':>18}: size={file_map_size} pages, max_entries={file_map_max_entries}, ecount={file_map_ecount}, entries={file_map_entries}"
        )

    def print_sector_tags(self, start_sector: int, end_sector: int):
        """
        Each tag is 12 or 20 bytes.

        Bytes 4,5 are the fileid type.

        The diffent fileid types were found at
        https://github.com/theMK2k/DiscImageChef/blob/master/DiscImageChef.Filesystems/LisaFS/Consts.cs
        """
        safe_end_sector = (
            end_sector if (end_sector < self._num_sectors) else self._num_sectors
        )
        for i in range(start_sector, safe_end_sector):
            self._file.seek(
                DC42_HEADER_SIZE
                + self._num_sectors * SECTOR_SIZE_IN_BYTES
                + i * self._single_tag_size
            )
            tags_bytes = self.read_tags_for_sector(i)
            file_id_type = struct.unpack(">H", tags_bytes[4:6])[0]
            tags_as_hex_string = " ".join(f"{byte:02X}" for byte in tags_bytes)
            print(
                f"Tags for sector {i:#04}: {tags_as_hex_string} : file_id_type={file_id_type_to_string(file_id_type)}({file_id_type:#04x})"
            )

    def print_mddf_sector_info(self):
        mddf_sector_data_bytes = self._mddf_sector_bytes
        print(
            f"Printing info from the MDDF (Media Descriptor Data File) sector {self._mddf_sector_number}:"
        )
        print_fields_in_byte_array(MDDF_FIELD_DEFINITIONS, mddf_sector_data_bytes, 0)
        # fs_version = to_uint16_big_endian(mddf_sector_data_bytes, 0x00)
        # print(f"LisaFS: {file_system_version_to_string(fs_version)} ({fs_version:#04x})")
        # volumn_name_bytes = mddf_sector_data_bytes[0x0C:0x0C + 33]  # Take 33 bytes from index 0x0C
        # volumn_name = pascal_to_string(volumn_name_bytes)
        # print(f"Volume name: {volumn_name}")
        # s_records_start_sector_offset_after_mddf = to_uint32_big_endian(mddf_sector_data_bytes, 0x94)
        # print(f"S-Records data start sector offset (after the MDDF sector): {s_records_start_sector_offset_after_mddf} ({s_records_start_sector_offset_after_mddf:#010x})")
        # s_records_start_sector_number = self._mddf_sector_number + s_records_start_sector_offset_after_mddf
        # print(f"S-Records data start sector: {s_records_start_sector_number} ({s_records_start_sector_number:#010x})")

        # number_of_s_records_per_sector = to_uint16_big_endian(mddf_sector_data_bytes, 0x98) # Usually 36 (36 x 14 bytes per s-record = 504 bytes, which fits well into the available 512 sector bytes)
        # print(f"number_of_s_entries_per_block: {number_of_s_records_per_sector} ({number_of_s_records_per_sector:#06x})")

        # s_records_sectors_count = to_uint16_big_endian(mddf_sector_data_bytes, 0x9A)
        # print(f"s_records_sectors_count: {s_records_sectors_count} ({s_records_sectors_count:#06x})")

        # empty_file_start_sector_offset = to_uint16_big_endian(mddf_sector_data_bytes, 0x9e)
        # print(f"empty_file_start_sector_offset: {empty_file_start_sector_offset} ({empty_file_start_sector_offset:#06x})")

        # read_and_print_bytes_in_hex_and_ascii(file, HEADER_SIZE + self._mddf_sector_number*512, 512)
        print(f"")

    def print_allocation_bitmap_sector_info(
        self, file: BufferedReader, allocation_bitmap_sector_numbers: List[int]
    ):
        if len(allocation_bitmap_sector_numbers) == 0:
            print(f"\nAllocation bitmap sectors could not be found in this file!\n")
            return

        for allocation_bitmap_sector_sector_number in allocation_bitmap_sector_numbers:
            print(
                f"Found allocation bitmap block at sector {allocation_bitmap_sector_sector_number} ; it starts at offset  {(DC42_HEADER_SIZE + allocation_bitmap_sector_sector_number*512):#08x}\n"
            )

    def read_sector(self, sector_number: int) -> bytes:
        """Reads the 512 bytes of sector data for the given sector number (0-based).

        The sector_number being passed is an "absolute" sector number, meaning the first sector of the disk image is sector 0,
        vs a "relative" sector number, which is the sector number relative to the MDDF sector (the first sector of the disk image).
        """
        if sector_number >= self._num_sectors:
            raise ValueError(
                f"Sector number {sector_number} is out of bounds, because the file has only {self._num_sectors} sectors!"
            )

        if self._is_dc42_format:
            position: int = DC42_HEADER_SIZE + sector_number * SECTOR_SIZE_IN_BYTES
        else:
            # The raw image file format is: a set of sectors, where each sector has 20-bytes tag data followed by 512 bytes sector data.
            position: int = (
                interleave5(sector_number)
                * (SECTOR_SIZE_IN_BYTES + self._single_tag_size)
                + self._single_tag_size
            )
        self._file.seek(position)
        return self._file.read(SECTOR_SIZE_IN_BYTES)

    def read_4_sectors(self, sector_number: int):
        """Useful for reading catalog sectors, which always come in sets of 4 consecutive sectors."""
        result = b""
        for i in range(sector_number, sector_number + 4):
            sector_bytes = self.read_sector(i)
            result += sector_bytes
        return result

    def read_tags_for_sector(self, sector_number: int):
        if sector_number >= self._num_sectors:
            raise ValueError(
                f"Sector number {sector_number} is out of bounds, because the file has only {self._num_sectors} sectors!"
            )

        if self._is_dc42_format:
            position: int = (
                DC42_HEADER_SIZE
                + self._num_sectors * SECTOR_SIZE_IN_BYTES
                + sector_number * self._single_tag_size
            )
        else:
            # The raw image file format is: a set of sectors, where each sector has 20-bytes tag data followed by 512 bytes sector data.
            position: int = interleave5(sector_number) * (
                SECTOR_SIZE_IN_BYTES + self._single_tag_size
            )
        self._file.seek(position)
        tag_bytes = self._file.read(self._single_tag_size)
        if (
            len(tag_bytes) < self._single_tag_size
        ):  # Creates a byte array of "FF....FF" for invalid sector numbers.
            padding_needed = self._single_tag_size - len(tag_bytes)
            ff_padding = b"\xff" * padding_needed
            tag_bytes = tag_bytes + ff_padding
        return tag_bytes

    def read_tags_for_sector_as_hex_string(self, sector_number: int) -> str:
        tags_bytes = self.read_tags_for_sector(sector_number)
        hex_string = " ".join(f"{byte:02x}" for byte in tags_bytes)
        return hex_string

    def pretty_print_tags_for_sector(self, sector_number: int):
        """We can see what the tag values really mean in wswrite.c, function writeFileTagBytes(...)"""
        tags_bytes = self.read_tags_for_sector(
            sector_number
        )  # Either 12 or 20 bytes array
        if self._single_tag_size == 20:
            print(
                f"Tag field names:        |verision|vol_id|file_id|dataused|abs_num|checksum|rel_num|fwd_link|bkwd_link|"
            )
            print(
                f"Tags for sector {sector_number:<6}: |    {tags_bytes[0]:02x}{tags_bytes[1]:02x}|"  # verision (2 bytes)
                f"  {tags_bytes[2]:02x}{tags_bytes[3]:02x}|"  # vol_id (2 bytes)
                f"   {tags_bytes[4]:02x}{tags_bytes[5]:02x}|"  # file_id (2 bytes)
                f"   {tags_bytes[6]:02x}{tags_bytes[7]:02x}|"  # data used (2 bytes)
                f"  {tags_bytes[8]:02x}{tags_bytes[9]:02x}{tags_bytes[10]:02x}|"  # absolute sector number (meaning if counted from the mddf sector) (3 bytes)
                f"      {tags_bytes[11]:02x}|"  # tag checksum (1 byte)
                f"   {tags_bytes[12]:02x}{tags_bytes[13]:02x}|"  # relative sector number (2 bytes)
                f"  {tags_bytes[14]:02x}{tags_bytes[15]:02x}{tags_bytes[16]:02x}|"  # fwd_link (3 bytes)
                f"   {tags_bytes[17]:02x}{tags_bytes[18]:02x}{tags_bytes[19]:02x}|"  # bkwd_link (3 bytes)
            )
        elif self._single_tag_size == 12:
            # Floppy tag layout (per FINISH_READ in SOURCE-SONYASM.TEXT.unix.txt; no checksum field):
            #   version(2) vol_id(2) file_id(2) rel_num(2)
            #   fwd_link(11b)+dataused(5b) @8-9, bkwd_link(11b)+dataused(11b) @10-11
            # Links are MDDF-relative; 0x07FF = "no link".
            print(
                f"Tag field names:        |version |vol_id |file_id|rel_num|fwd_link+dataused|bkwd_link+dataused|"
            )
            print(
                f"Tags for sector {sector_number:<6}: |  {tags_bytes[0]:02x}{tags_bytes[1]:02x}|  {tags_bytes[2]:02x}{tags_bytes[3]:02x}|  {tags_bytes[4]:02x}{tags_bytes[5]:02x}|  {tags_bytes[6]:02x}{tags_bytes[7]:02x}|    {tags_bytes[8]:02x}{tags_bytes[9]:02x}        |    {tags_bytes[10]:02x}{tags_bytes[11]:02x}       |"
            )
            hex_string = " ".join(f"{byte:02x}" for byte in tags_bytes)
            print(hex_string)
        else:
            raise "Unknown tag size!"

    def get_file_id_from_tag_data_for_sector(self, sector_number: int) -> int:
        tag_bytes = self.read_tags_for_sector(sector_number)
        return struct.unpack(">H", tag_bytes[4:6])[0]

    def get_file_id_type_from_tag_data_for_sector(self, sector_number: int) -> str:
        file_id_type = self.get_file_id_from_tag_data_for_sector(sector_number)
        return f"{file_id_type_to_string(file_id_type)}"

    def is_sector_free_in_bitmap(self, absolute_sector_number: int) -> bool:
        """We will check if this sector in the bitmap data is free.

        absolute means that it is counted from sector 0, whereas
        relative means "it is counted from the mddf sector". Confusing yet?
        """
        rel_sector_number = absolute_sector_number - self._mddf_sector_number
        first_relative_bitmap_start_sector_number = to_uint32_big_endian(
            self._mddf_sector_bytes, 0x88
        )  # Stored in the MDDF sector at offsert 0x88 (136 decimal); usually set to 1, whi9ch means that the first bitmap sector is the one immediately after the MDDF sector
        first_abs_bitmap_start_sector_number = (
            first_relative_bitmap_start_sector_number + self._mddf_sector_number
        )
        num_bitmap_sectors = to_uint16_big_endian(
            self._mddf_sector_bytes, 0x92
        )  # Stored in the MDDF sector at offset 0x92 (146 decimal);
        relative_bitmap_sector_num = int(
            rel_sector_number / 4096
        )  # Each bitmap sector covers 4096 data sectors (512 bytes x 8 bits/byte = 4096 bits)
        if relative_bitmap_sector_num >= num_bitmap_sectors:
            print(
                f"############################ Warning: The total number of bitmap sectors is {num_bitmap_sectors}, but we are reaching beyond that, at relative sector number {relative_bitmap_sector_num} (zero-based)"
            )
        byte_index = int((rel_sector_number - relative_bitmap_sector_num * 4096) / 8)
        if byte_index < 0 or byte_index > 4095:
            raise ValueError(
                f"byte_index must be an integer between 0 and 4095, but got {byte_index} ! This code is wrong!"
            )
        bit_offset = (
            rel_sector_number - relative_bitmap_sector_num * 4096 - byte_index
        ) % 8
        if bit_offset < 0 or bit_offset > 7:
            raise ValueError(
                f"bit_offset must be an integer between 0 and 7, but got {bit_offset} ! This code is wrong!"
            )
        bit_mask = 1 << bit_offset

        bitmap_sector_bytes = self.read_sector(
            first_abs_bitmap_start_sector_number + relative_bitmap_sector_num
        )
        bitmap_byte = bitmap_sector_bytes[byte_index]
        is_free = False if (bitmap_byte & bit_mask != 0) else True
        return is_free

    def dump_free_bitmap_sectors_numbers(self):
        num_bits_in_allocation_bitmap = to_uint32_big_endian(
            self._mddf_sector_bytes, 0x8C
        )  # Stored in the MDDF sector at offset 0x8C (140 decimal); there is one bit per sector, so it is usually set to the total number of sectors in the volume  minus the MDDF sector number.
        first_relative_bitmap_start_sector_number = to_uint32_big_endian(
            self._mddf_sector_bytes, 0x88
        )  # Stored in the MDDF sector at offsert 0x88 (136 decimal); usually set to 1, whi9ch means that the first bitmap sector is the one immediately after the MDDF sector
        first_abs_bitmap_start_sector_number = (
            first_relative_bitmap_start_sector_number + self._mddf_sector_number
        )
        num_bitmap_sectors = to_uint16_big_endian(
            self._mddf_sector_bytes, 0x92
        )  # Stored in the MDDF sector at offset 0x92 (146 decimal);
        print(
            f"\nDumping all free sector numbers from the bitmap sectors (with numbers from {first_abs_bitmap_start_sector_number} to {first_abs_bitmap_start_sector_number + num_bitmap_sectors -1}) : "
        )
        for sector in range(
            first_abs_bitmap_start_sector_number,
            first_abs_bitmap_start_sector_number + num_bitmap_sectors,
        ):
            bitmap_sector_bytes = self.read_sector(sector)
            for byte_index in range(0, 512):
                byte = bitmap_sector_bytes[byte_index]
                for bit_offset in range(0, 8):
                    bit_mask = 1 << bit_offset
                    absolute_sector_number = (
                        (sector - first_abs_bitmap_start_sector_number) * 4096
                        + byte_index * 8
                        + bit_offset
                    )
                    if (
                        absolute_sector_number
                        > num_bits_in_allocation_bitmap + self._mddf_sector_number
                    ):
                        print(
                            f"Total number of free sectors found in the bitmap: {num_free_bitmap_sectors_numbers}"
                        )
                        return
                    is_free: bool = False if (byte & bit_mask != 0) else True
                    if is_free:
                        print(f"Absolute sector {absolute_sector_number} is FREE")
                        num_free_bitmap_sectors_numbers += 1
        print(
            f"Total number of free sectors found in the bitmap: {num_free_bitmap_sectors_numbers}"
        )

    def dump_bitmap_sectors(self):
        first_relative_bitmap_start_sector_number = to_uint32_big_endian(
            self._mddf_sector_bytes, 0x88
        )  # Stored in the MDDF sector at offsert 0x88 (136 decimal); usually set to 1, whi9ch means that the first bitmap sector is the one immediately after the MDDF sector
        bitmap_start_sector_number = (
            first_relative_bitmap_start_sector_number + self._mddf_sector_number
        )
        num_bitmap_sectors = to_uint16_big_endian(
            self._mddf_sector_bytes, 0x92
        )  # Stored in the MDDF sector at offset 0x92 (146 decimal);
        print(
            f"\nDumping all bitmap sectors (with numbers from {bitmap_start_sector_number} to {bitmap_start_sector_number + num_bitmap_sectors -1}) : "
        )
        for sector in range(
            bitmap_start_sector_number, bitmap_start_sector_number + num_bitmap_sectors
        ):
            sector_file_id = self.get_file_id_from_tag_data_for_sector(sector)
            if sector_file_id != 0x0002:
                print(
                    f"WARNING: Expected bitmap sector to have file_id=0x0002, but found file_id={sector_file_id:#06x} instead!"
                )
            print(
                f"Dumping bitmap sector {sector} with file_id={self.get_file_id_from_tag_data_for_sector(sector):#06x} :"
            )
            bitmap_sector_bytes = self.read_sector(sector)
            print_bytes_in_hex_and_ascii(bitmap_sector_bytes)

    def _dump_destination(self, file_name: str, flatten: bool = False):
        """Compute the host file path a Lisa file is dumped to under /tmp/LisaFileSystemDump.

        By default a '/' in the Lisa file name becomes a host folder separator: a
        file named 'apbg/BG1A.TEXT' is written into the apbg/ subfolder of the dump
        folder. With flatten=True the '/' is replaced with '-' instead ('apbg-BG1A.TEXT'),
        so every file is written directly into the dump folder itself.

        Returns the full output path, or None if the (possibly flattened) name would
        escape the dump folder (a leading '/' or a '..' component) and must be skipped.
        """
        output_root = "/tmp/LisaFileSystemDump"
        host_name = file_name.replace("/", "-") if flatten else file_name
        output_filename = os.path.normpath(os.path.join(output_root, host_name))
        if not output_filename.startswith(output_root + os.sep):
            return None
        return output_filename

    def dump_files(self, flatten: bool = False):
        """
        Dumps all files from the dc42 image to the host file system, into folder /tmp/LisaFileSystemDump (it creates the folder if not present).
        By default it preserves the directory structure: a '/' in a Lisa file name
        becomes a subfolder. With flatten=True, a '/' is replaced with '-' instead, so
        all files are dumped into the single /tmp/LisaFileSystemDump folder (the 'dump-flatten'
        command). Does not attempt to rename the files in any other way.

        How it works:
        Read each s-record sector (each such sector contains 36 s-records, one per file).
        Each s-record is of 14 bytes:
         - 4 bytes for the file's "hint sector number" (a sector which contains the file name), aka "s_entry.hintaddr" in Lisa sources file LISA_OS/OS/source-sfileio.text.unix.txt
         - 4 bytes for the file's content start sector number,                                  aka "s_entry.fileaddr" in Lisa sources file LISA_OS/OS/source-sfileio.text.unix.txt
         - 4 bytes for the file size,                                                           aka "s_entry.filesize" in Lisa sources file LISA_OS/OS/source-sfileio.text.unix.txt
         - 2 bytes for the file version (typically we see "0001"),                              aka "s_entry.version"  in Lisa sources file LISA_OS/OS/source-sfileio.text.unix.txt
        To read the full file contents, we start at it's file content start sector number (above); it's tag data has the next file content sector number;
        We follow the tag data until we find a next sector number of 00FFFFFF, which indicates that we have finished reading the file contents.
        Note: the number_of_s_records_per_sector (which we read from the MDDF sector) is usually 36: (36 x 14 bytes per s-record = 504 bytes, which fits well into the available 512 sector bytes)

        There is another way (used by lisafsh-tool.c):
        Read each catalog sector (each such sector contains 8 catalog records, one per file).
        Each s-record is of 64 bytes which contain the file name and file_id.
        To read the file contents for the current catalog entry: We find all sectors whose tags have the given file_id, and we read them.

        """
        if self.is_flat_catalog_volume():
            # fs_version 14/15 volumes: there is no B-tree catalog, so use the slist + tag chains instead:
            self._dump_files_flat_catalog(flatten)
            return
        mddf_sector_bytes = self._mddf_sector_bytes
        s_records_start_sector_offset_after_mddf = to_uint32_big_endian(
            mddf_sector_bytes, 0x94
        )
        # print(f"S-Records data start sector offset (after the MDDF sector): {s_records_start_sector_offset_after_mddf} ({s_records_start_sector_offset_after_mddf:#010x})")
        s_records_start_sector_number = (
            self._mddf_sector_number + s_records_start_sector_offset_after_mddf
        )
        # print(f"S-Records data start sector number: {s_records_start_sector_number} ({s_records_start_sector_number:#010x})")

        number_of_s_records_per_sector = to_uint16_big_endian(
            mddf_sector_bytes, 0x98
        )  # Usually 36 (36 x 14 bytes per s-record = 504 bytes, which fits well into the available 512 sector bytes)
        # print(f"number_of_s_entries_per_block: {number_of_s_records_per_sector} ({number_of_s_records_per_sector:#06x})")

        s_records_sectors_count = to_uint16_big_endian(mddf_sector_bytes, 0x9A)
        # print(f"s_records_sectors_count: {s_records_sectors_count} ({s_records_sectors_count:#06x})")

        # Read each s-record sector (each such sector contains number_of_s_records_per_sector=36 s-records, one per file).
        # each s-record is of 14 bytes:
        #  - 4 bytes for the file's "hint sector number" (a sector which contains the file name), aka "s_entry.hintaddr" in Lisa sources file LISA_OS/OS/source-sfileio.text.unix.txt
        #  - 4 bytes for the file's content start sector number,                                  aka "s_entry.fileaddr" in Lisa sources file LISA_OS/OS/source-sfileio.text.unix.txt
        #  - 4 bytes for the file size,                                                           aka "s_entry.filesize" in Lisa sources file LISA_OS/OS/source-sfileio.text.unix.txt
        #  - 2 bytes for the file version (typically we see "0001"),                              aka "s_entry.version"  in Lisa sources file LISA_OS/OS/source-sfileio.text.unix.txt
        # To read the full file contents, we start at it's file_content_start_sector_number; it's tag data has the next file content sector number;
        # so we follow the tag data until we find a next sector number of 00FFFFFF, which indicates that we have finished reading the file contents.
        for sector in range(
            s_records_start_sector_number,
            (s_records_start_sector_number + s_records_sectors_count),
        ):
            # print(f"At s-record sector {sector:#6} of type {self.get_file_id_type_from_tag_data_for_sector(sector)}")
            sector_bytes = self.read_sector(sector)
            # print_bytes_in_hex_and_ascii(sector_bytes)
            for s_record_index in range(0, number_of_s_records_per_sector):
                s_record_data_offset_within_sector = s_record_index * 14
                # each s-record is 14 bytes
                # print_bytes_in_hex_and_ascii(sector_bytes[s_record_data_offset_within_sector : s_record_data_offset_within_sector + 14])
                # The first 4 bytes of the s-record are the "hint sector number":
                hint_sector_number = (
                    to_uint32_big_endian(
                        sector_bytes, s_record_data_offset_within_sector
                    )
                    + self._mddf_sector_number
                )
                if hint_sector_number > self._num_sectors:
                    # The first 5 s-records have long, weird hint_sector numbers, e.g. 4294967333. Skip them.
                    print(
                        f"Skipping reading too-large hint sector number {hint_sector_number}"
                    )
                    continue

                # The next 4 bytes of the s-record are the sector offset (from the mddf sector) where the filee content starts:
                file_content_start_sector_offset = to_uint32_big_endian(
                    sector_bytes, s_record_data_offset_within_sector + 4
                )
                if file_content_start_sector_offset == 0x00000000:
                    # print("\n");
                    continue
                file_content_start_sector_number = (
                    file_content_start_sector_offset + self._mddf_sector_number
                )
                # Each hint sector describes one file. Observation: each hint sector file id is different, e.g. 0xfeb9, 0xfeba, 0xfebb
                # print(f"  At hint sector number {hint_sector_number:#6} with file_id {self.get_file_id_type_from_tag_data_for_sector(hint_sector_number)}")
                hint_sector_bytes = self.read_sector(hint_sector_number)
                file_name = pascal_to_string(hint_sector_bytes, start=0)
                file_size = to_uint32_big_endian(
                    sector_bytes, s_record_data_offset_within_sector + 8
                )
                file_version = to_uint16_big_endian(
                    sector_bytes, s_record_data_offset_within_sector + 12
                )
                print(
                    f"s_record_index={s_record_index}, file_data_start_sector_number={file_content_start_sector_number}, hint_sector_number={hint_sector_number},"
                    f" file_name={file_name}, file_size={file_size} bytes, file_version={file_version}"
                )
                if file_name == "" or "\x00" in file_name:
                    # The file name (from a stale/corrupt hint sector) is empty or contains a
                    # character that cannot appear in a Linux file name: skip it rather than crash.
                    print(
                        f"Skipping file with an invalid file name {file_name!r}: s_record_index={s_record_index}"
                    )
                    continue

                # Lisa does not treat '/' as a folder delimiter in file names, but
                # when saving to the host file system we use it as one (or replace it
                # with '-' in flatten mode). Skip any name that would otherwise escape
                # the dump folder (a leading '/' or '..' components).
                output_filename = self._dump_destination(file_name, flatten)
                if output_filename is None:
                    print(
                        f"Skipping file with an unsafe file name {file_name!r}: s_record_index={s_record_index}"
                    )
                    continue
                # Ensure the output directory exists
                output_dir = os.path.dirname(output_filename)
                if output_dir and not os.path.exists(output_dir):
                    os.makedirs(output_dir)
                    print(f"Created directory: {output_dir}")
                # Lisa OS text files are special: the on-disk layout is a 1024-byte
                # header page (formatting metadata, not part of the file's contents)
                # followed by 1024-byte pages of CR-terminated (0x0D) lines, each
                # page null-padded after its last line (the first CR-null pair in a
                # page ends the page). For file names ending in ".TEXT" (case-
                # insensitive), we dump the inverse of that layout as plain host text:
                # strip the header page and the null page padding, and convert the CR
                # line endings to host "\n" (see lisa_text_file_to_host_text()).
                is_text_file = file_name.upper().endswith(".TEXT")
                with open(output_filename, "wb") as output_file_handler:
                    next_sector_to_read = file_content_start_sector_number
                    iteration_number = (
                        0  # Safety measure to prevent infinite loops below:
                    )
                    total_bytes_read = 0
                    raw_text_file_bytes = bytearray() if is_text_file else None
                    while True:
                        print(
                            f"  At file content sector {next_sector_to_read:#6} with file_id {self.get_file_id_type_from_tag_data_for_sector(next_sector_to_read)}"
                        )
                        file_content_sector_bytes = self.read_sector(
                            next_sector_to_read
                        )
                        if is_text_file:
                            # Accumulate the whole chain (including the two header
                            # sectors): the on-disk text layout is converted to host
                            # text only after the last sector has been read.
                            raw_text_file_bytes += file_content_sector_bytes
                        else:
                            # The file content starts at the very first sector of the
                            # chain (the s-record's fileaddr points at the file's
                            # first data sector). The last sector is usually only
                            # partially used; we truncate to the file size below.
                            output_file_handler.write(file_content_sector_bytes)
                        total_bytes_read += len(file_content_sector_bytes)

                        tags = self.read_tags_for_sector(next_sector_to_read)
                        # The fwd_link (next sector in the file's data chain) is 3 bytes at tag offsets
                        # 0x0E..0x10 in 20-byte hard-disk tags, and the low 11 bits of tag bytes 8-9 in
                        # 12-byte floppy tags; the shared helper decodes both tag sizes:
                        _, next_sector_from_tag_data, end_sentinel = (
                            self._decode_tag_dataused_and_fwd_link(tags)
                        )

                        if next_sector_from_tag_data == end_sentinel:
                            # print(f"  Exiting while true loop, as we found next_sector_from_tag_data={next_sector_from_tag_data:#010x}")
                            break
                        next_sector_to_read = (
                            next_sector_from_tag_data + self._mddf_sector_number
                        )
                        iteration_number += 1
                        if iteration_number > 10000:
                            raise f"Infinite loop detected while reading contents of file_id {self.get_file_id_type_from_tag_data_for_sector(next_sector_to_read)}, file_name '{file_name}'! Perhaps the file system is corrupted? Exiting."
                    if total_bytes_read < file_size:
                        print(
                            f"WARNING: the sector chain of file '{file_name}' is only {total_bytes_read} bytes long, shorter than its original file size of {file_size}!"
                        )
                    if is_text_file:
                        # The last sector is only partially used: cut the raw data down
                        # to the file size before converting (the header page is
                        # stripped by lisa_text_file_to_host_text()):
                        if file_size < len(raw_text_file_bytes):
                            del raw_text_file_bytes[file_size:]
                        output_file_handler.write(
                            lisa_text_file_to_host_text(bytes(raw_text_file_bytes))
                        )
                    elif total_bytes_read > file_size:
                        # The file's last sector is only partially used: cut the dump down to the file size:
                        output_file_handler.truncate(file_size)

    def check_bitmap_for_all_file_data(self):
        """ """
        print(
            "\nChecking the allocation bitmap, to see if it appears occupied for all file data sectors ..."
        )
        mddf_sector_bytes = self._mddf_sector_bytes
        s_records_start_sector_offset_after_mddf = to_uint32_big_endian(
            mddf_sector_bytes, 0x94
        )
        # print(f"S-Records data start sector offset (after the MDDF sector): {s_records_start_sector_offset_after_mddf} ({s_records_start_sector_offset_after_mddf:#010x})")
        s_records_start_sector_number = (
            self._mddf_sector_number + s_records_start_sector_offset_after_mddf
        )
        # print(f"S-Records data start sector number: {s_records_start_sector_number} ({s_records_start_sector_number:#010x})")

        number_of_s_records_per_sector = to_uint16_big_endian(
            mddf_sector_bytes, 0x98
        )  # Usually 36 (36 x 14 bytes per s-record = 504 bytes, which fits well into the available 512 sector bytes)
        # print(f"number_of_s_entries_per_block: {number_of_s_records_per_sector} ({number_of_s_records_per_sector:#06x})")

        s_records_sectors_count = to_uint16_big_endian(mddf_sector_bytes, 0x9A)
        # print(f"s_records_sectors_count: {s_records_sectors_count} ({s_records_sectors_count:#06x})")

        # Read each s-record sector (each such sector contains number_of_s_records_per_sector=36 s-records, one per file).
        # each s-record is of 14 bytes:
        #  - 4 bytes for the file's "hint sector number" (a sector which contains the file name), aka "s_entry.hintaddr" in Lisa sources file LISA_OS/OS/source-sfileio.text.unix.txt
        #  - 4 bytes for the file's content start sector number,                                  aka "s_entry.fileaddr" in Lisa sources file LISA_OS/OS/source-sfileio.text.unix.txt
        #  - 4 bytes for the file size,                                                           aka "s_entry.filesize" in Lisa sources file LISA_OS/OS/source-sfileio.text.unix.txt
        #  - 2 bytes for the file version (typically we see "0001"),                              aka "s_entry.version"  in Lisa sources file LISA_OS/OS/source-sfileio.text.unix.txt
        # To read the full file contents, we start at it's file_content_start_sector_number; it's tag data has the next file content sector number;
        # so we follow the tag data until we find a next sector number of 00FFFFFF, which indicates that we have finished reading the file contents.
        for sector in range(
            s_records_start_sector_number,
            (s_records_start_sector_number + s_records_sectors_count),
        ):
            # print(f"At s-record sector {sector:#6} of type {self.get_file_id_type_from_tag_data_for_sector(sector)}")
            sector_bytes = self.read_sector(sector)
            # print_bytes_in_hex_and_ascii(sector_bytes)
            for s_record_index in range(0, number_of_s_records_per_sector):
                s_record_data_offset_within_sector = s_record_index * 14
                # each s-record is 14 bytes
                # print_bytes_in_hex_and_ascii(sector_bytes[s_record_data_offset_within_sector : s_record_data_offset_within_sector + 14])
                # The first 4 bytes of the s-record are the "hint sector number":
                hint_sector_number = (
                    to_uint32_big_endian(
                        sector_bytes, s_record_data_offset_within_sector
                    )
                    + self._mddf_sector_number
                )
                if hint_sector_number > self._num_sectors:
                    # The first 5 s-records have long, weird hint_sector numbers, e.g. 4294967333. Skip them.
                    print(
                        f"Skipping reading too-large hint sector number {hint_sector_number}"
                    )
                    continue

                # The next 4 bytes of the s-record are the sector offset (from the mddf sector) where the filee content starts:
                file_content_start_sector_offset = to_uint32_big_endian(
                    sector_bytes, s_record_data_offset_within_sector + 4
                )
                if file_content_start_sector_offset == 0x00000000:
                    # print("\n");
                    continue
                abs_file_content_start_sector_number = (
                    file_content_start_sector_offset + self._mddf_sector_number
                )
                # Each hint sector describes one file. Observation: each hint sector file id is different, e.g. 0xfeb9, 0xfeba, 0xfebb
                # print(f"  At hint sector number {hint_sector_number:#6} with file_id {self.get_file_id_type_from_tag_data_for_sector(hint_sector_number)}")
                hint_sector_bytes = self.read_sector(hint_sector_number)
                file_name = pascal_to_string(hint_sector_bytes, start=0)
                file_size = to_uint32_big_endian(
                    sector_bytes, s_record_data_offset_within_sector + 8
                )
                file_version = to_uint16_big_endian(
                    sector_bytes, s_record_data_offset_within_sector + 12
                )
                print(
                    f"\ns_record_index={s_record_index}, file_data_start_sector_number={abs_file_content_start_sector_number}, hint_sector_number={hint_sector_number},"
                    f" file_name={file_name}, file_size={file_size} bytes, file_version={file_version}"
                )

                next_abs_sector_to_read = abs_file_content_start_sector_number  # start with this, then we update it in the loop below
                iteration_number = 0  # Safety measure to prevent infinite loops below:

                while True:
                    is_sector_free_in_bitmap = self.is_sector_free_in_bitmap(
                        next_abs_sector_to_read
                    )
                    print(
                        f"  At file content absolute sector {next_abs_sector_to_read:#6} with file_id {self.get_file_id_type_from_tag_data_for_sector(next_abs_sector_to_read)}, is_sector_free_in_bitmap={is_sector_free_in_bitmap}"
                    )

                    if is_sector_free_in_bitmap:
                        print(
                            f"################### This ain't right: sector {next_abs_sector_to_read:#6} is marked as free in the bitmap, but it is used by file '{file_name}' !"
                        )
                        # raise ValueError("This ain't right ...")

                    tags = self.read_tags_for_sector(next_abs_sector_to_read)
                    # The fwd_link (next sector in the file's data chain) is 3 bytes at tag offsets
                    # 0x0E..0x10 in 20-byte hard-disk tags, and the low 11 bits of tag bytes 8-9 in
                    # 12-byte floppy tags; the shared helper decodes both tag sizes:
                    _, next_sector_from_tag_data, end_sentinel = (
                        self._decode_tag_dataused_and_fwd_link(tags)
                    )

                    if next_sector_from_tag_data == end_sentinel:
                        # print(f"  Exiting while true loop, as we found next_sector_from_tag_data={next_sector_from_tag_data:#010x}")
                        break
                    next_abs_sector_to_read = (
                        next_sector_from_tag_data + self._mddf_sector_number
                    )
                    iteration_number += 1
                    if iteration_number > 10000:
                        raise f"Infinite loop detected while reading contents of file_id {self.get_file_id_type_from_tag_data_for_sector(next_abs_sector_to_read)}, file_name '{file_name}'! Perhaps the file system is corrupted? Exiting."

    def find_catalog_root_page_sector_number(self):
        """
        Returns the first sector of the catalog entries (aka "root catalog sector number). The returned sector number is relative to the MDDF sector,
        meaning that the absolute sector number is the returned number + self._mddf_sector_number
        """
        # this approach worked until I found a profile image file where we got pointed at sector 1737 which is a hint sector for one of the files (not a catalog sector) ...
        mddf_sector_bytes = self._mddf_sector_bytes
        catalog_root_page_sector_number = to_uint32_big_endian(
            mddf_sector_bytes, 0x12E
        )  # The first catalog sector number (relative to the MDDF sector) is stored at offset 0x12E = 302 in the MDDF sector
        # if self.get_file_id_from_tag_data_for_sector(catalog_root_page_sector_number) ==  0x0004:
        #    print(f"HERE 1")
        return catalog_root_page_sector_number
        # Alternate way:
        # for sector_number in range(self._mddf_sector_number, self._num_sectors):
        #     file_id = self.get_file_id_from_tag_data_for_sector(sector_number)
        #     if file_id == 0x0004:
        #         print(f"HERE 3")
        #         return sector_number
        # raise "While looking for the root catalog sector: could not find ANY sector whose file_id is 0x0004 (a catalog). Cannot continue, exiting."

    def dump_catalog(self):
        """
        Dump every entry (files, directories, threads, ...) in the HFS B-tree catalog.

        Catalog page numbers are MDDF-relative: the absolute sector number of a
        catalog node is self._mddf_sector_number + page_number.

        Catalog node layout (2048 bytes, always 4 consecutive sectors), as implemented by the Lisa OS
        (see LISA_OS/OS/source-fsdir.text.unix.txt, LISA_OS/OS/source-fsasm.text.unix.txt and
        LISA_OS/OS/source-scavenger.text.unix.txt):

          * Records start at offset 0 and grow towards the end of the node.
          * The offset table holds the start offset (2-byte big-endian) of record i at node offset
            2034 - 2*i. The entry at index nkeys (i.e. at 2034 - 2*nkeys) is the "used" sentinel,
            i.e. the end offset of the last record.
          * The node descriptor is at offset 2036:
                nkeys (2 bytes) @ 2036, prior (4 bytes) @ 2038, next (4 bytes) @ 2042,
                kind (1 byte) @ 2046 (0 = leaf node, 1 = index node), cksum (1 byte) @ 2047.
          * An index node's records are: [child page number (4 bytes, MDDF-relative)][key (36 bytes)].
          * A leaf node's records are: [key (36 bytes)][eType (2 bytes)][type-specific fields].
          * The key is: [0x24][parent ID (2 bytes, big-endian)][name (up to 32 bytes, zero padded)][0x00].

        Traversal (mirrors FirstRec/SeqRec in source-fsdir.text.unix.txt):
          * If the root node is an index node, descend to the leftmost child (the page number in the
            first record), repeating until a leaf node is reached.
          * Then enumerate all records of the leaf, and follow the 'next' pointer from the node
            descriptor (a MDDF-relative page number) until it is BAD (0xFFFFFFFF).
          * Works with any B-tree depth (the Lisa OS's NodeStack is indexed array[0..15],
            and the MDDF's tree_depth is a 2-byte field, so in practice depths well beyond 2
            are possible on large hard-disk volumes).

        More about the "thread" Lisa HFS catalog record type: it represents a directory (one
        per directory). Every HFS volume has at least one: the thread of the root directory,
        which is the first record in the catalog — that's the thread entry for '' (parent_id=0)
        """
        catalog_root_page_sector_number = self.find_catalog_root_page_sector_number()
        absolute_catalog_root_page_sector_number = (
            catalog_root_page_sector_number + self._mddf_sector_number
        )
        print(
            f"\nDumping the catalog, which starts at absolute sector number {absolute_catalog_root_page_sector_number} :"
        )
        print(
            f"relative_catalog_root_page_sector_number={catalog_root_page_sector_number} has file_id {self.get_file_id_type_from_tag_data_for_sector(absolute_catalog_root_page_sector_number)}"
        )
        # print(f"")
        # self.pretty_print_tags_for_sector(absolute_catalog_root_page_sector_number)
        # print(f"")
        # print_bytes_in_hex_and_ascii(self.read_sector(absolute_catalog_root_page_sector_number))
        # print(f"")

        # Step 1: descend from the root to the leftmost leaf node (only needed if the tree depth > 1)
        current_page = catalog_root_page_sector_number
        descended_to_leaf = False
        visited_descent_pages: set = set()
        while True:
            if current_page in visited_descent_pages:
                print(
                    f"WARNING: cycle detected while descending the index nodes at relative page {current_page}; stopping."
                )
                break
            visited_descent_pages.add(current_page)
            node_bytes = self.read_4_sectors(self._mddf_sector_number + current_page)
            node_kind = node_bytes[(4 * 512) - 2]  # kind @ 2046: 0 = leaf, 1 = index
            if node_kind == 0:
                descended_to_leaf = True
                break  # reached a leaf node
            # Index node: the first record (at offset-table entry 0, i.e. node offset 0) starts with
            # the 4-byte MDDF-relative page number of the leftmost child node:
            first_record_offset = to_uint16_big_endian(node_bytes, (4 * 512) - 14)
            child_page = to_uint32_big_endian(node_bytes, first_record_offset)
            print(
                f"Catalog node at relative page {current_page} (absolute sector {self._mddf_sector_number + current_page}) is an index node; descending to leftmost child page {child_page}."
            )
            if child_page == current_page or child_page == 0xFFFFFFFF:
                print(
                    "WARNING: index node points at itself (or BAD); stopping the descent."
                )
                break
            current_page = child_page
        if not descended_to_leaf:
            print(
                f"################# Could not descend to a leaf node; found no catalog entries. ##############"
            )
            return

        # Step 2: walk the chain of leaf nodes, dumping all records
        file_rows: list = []  # rows for the file table printed at the end
        num_files_found = 0
        num_directories_found = 0
        num_threads_found = 0
        num_other_found = 0
        visited_pages: set = set()
        while current_page not in visited_pages:
            visited_pages.add(current_page)
            node_bytes = self.read_4_sectors(self._mddf_sector_number + current_page)
            num_entries = to_uint16_big_endian(
                node_bytes, (4 * 512) - 12
            )  # nkeys @ 2036
            next_page = to_uint32_big_endian(node_bytes, (4 * 512) - 6)  # next @ 2042
            node_kind = node_bytes[(4 * 512) - 2]  # kind @ 2046

            print(
                f"###################################################################### At catalog leaf node at relative page {current_page} "
                f"(absolute sector {self._mddf_sector_number + current_page}) with file_id={self.get_file_id_type_from_tag_data_for_sector(self._mddf_sector_number + current_page)}, "
                f"num_catalog_entries={num_entries}"
            )

            if node_kind != 0:
                print(
                    f"WARNING: expected a leaf node at relative page {current_page}, but kind={node_kind}. Stopping."
                )
                break

            # Dump the catalog entries found in the current node, using the offset table:
            for i in range(num_entries):
                record_start = to_uint16_big_endian(
                    node_bytes, (4 * 512) - 14 - (2 * i)
                )
                record_end = to_uint16_big_endian(
                    node_bytes, (4 * 512) - 14 - (2 * (i + 1))
                )  # for the last record this is the 'used' sentinel
                if record_start == 0xFFFF or record_end <= record_start:
                    print(
                        f"  WARNING: bad offset table entry for record {i+1} (start {record_start:#06x}, end {record_end:#06x}); skipping it."
                    )
                    continue
                record_bytes = node_bytes[record_start:record_end]
                entry_category = self.print_catalog_record(
                    record_bytes, i + 1, file_rows
                )
                if entry_category == "file":
                    num_files_found += 1
                elif entry_category == "directory":
                    num_directories_found += 1
                elif entry_category == "thread":
                    num_threads_found += 1
                else:
                    num_other_found += 1

            if next_page == 0xFFFFFFFF or next_page == 0 or next_page == current_page:
                print(
                    f"Found next_page={next_page:#010x}, which indicates the end of the catalog."
                )
                break
            current_page = next_page

        if file_rows:
            print()
            for line in format_table(
                ["s_file_id", "file_name", "size", "created", "last_modified"],
                file_rows,
            ):
                print(line)
        print(
            f"################# Found {num_files_found} files, {num_directories_found} directories, {num_threads_found} thread entries and {num_other_found} other catalog entries. ##############"
        )

    def find_protected_files(self) -> list[int]:
        """
        Walk the HFS B-tree catalog (the same traversal as dump_catalog()) and check the
        "protected" flag of every file. The flag is NOT in the catalog record: for each
        FILEENTRY we look up the file's hint (hentry) sector via its s_file_id (see
        get_sentry_for_sfile()) and read the byte at offset 0x48 of that sector - the
        third of the seven 1-byte boolean flags that follow machine_id in the hentry
        record (killed, safety_on, protected, master, scavenged, closed_by_OS, file_open;
        see LISA_OS/OS/source-fsprim.text.unix.txt and print_hint_sector_info() above).
        A protected file can only be opened on the machine whose serial number is stored
        in its machine_id field (theft protection, GOPEN in source-fsui1.text.unix.txt).

        Prints the name and s-file id of each protected file found, and returns the list
        of their absolute hint sector numbers.
        """
        print(
            "\nLooking for protected files (the 'protected' flag, byte 0x48, in each file's hint sector) ..."
        )
        protected_hint_sector_numbers: list = []

        if self.is_flat_catalog_volume():
            # fs_version 14/15 volumes have no B-tree catalog; scan the slist instead:
            return self._find_protected_files_flat_catalog()

        # Step 1: descend from the root to the leftmost leaf node (only needed if the tree depth > 1),
        # exactly as in dump_catalog():
        current_page = self.find_catalog_root_page_sector_number()
        descended_to_leaf = False
        visited_descent_pages: set = set()
        num_files_found = 0
        while True:
            if current_page in visited_descent_pages:
                print(
                    f"WARNING: cycle detected while descending the index nodes at relative page {current_page}; stopping."
                )
                break
            visited_descent_pages.add(current_page)
            node_bytes = self.read_4_sectors(self._mddf_sector_number + current_page)
            node_kind = node_bytes[(4 * 512) - 2]  # kind @ 2046: 0 = leaf, 1 = index
            if node_kind == 0:
                descended_to_leaf = True
                break  # reached a leaf node
            # Index node: the first record (at offset-table entry 0, i.e. node offset 0) starts with
            # the 4-byte MDDF-relative page number of the leftmost child node:
            first_record_offset = to_uint16_big_endian(node_bytes, (4 * 512) - 14)
            child_page = to_uint32_big_endian(node_bytes, first_record_offset)
            if child_page == current_page or child_page == 0xFFFFFFFF:
                print(
                    "WARNING: index node points at itself (or BAD); stopping the descent."
                )
                break
            current_page = child_page
        if not descended_to_leaf:
            print(
                f"################# Could not descend to a leaf node; found no files. ##############"
            )
            return protected_hint_sector_numbers

        # Step 2: walk the chain of leaf nodes, checking every file entry:
        visited_pages: set = set()
        while current_page not in visited_pages:
            visited_pages.add(current_page)
            node_bytes = self.read_4_sectors(self._mddf_sector_number + current_page)
            num_entries = to_uint16_big_endian(
                node_bytes, (4 * 512) - 12
            )  # nkeys @ 2036
            next_page = to_uint32_big_endian(node_bytes, (4 * 512) - 6)  # next @ 2042
            node_kind = node_bytes[(4 * 512) - 2]  # kind @ 2046
            if node_kind != 0:
                print(
                    f"WARNING: expected a leaf node at relative page {current_page}, but kind={node_kind}. Stopping."
                )
                break

            # Check the catalog entries found in the current node, using the offset table:
            for i in range(num_entries):
                record_start = to_uint16_big_endian(
                    node_bytes, (4 * 512) - 14 - (2 * i)
                )
                record_end = to_uint16_big_endian(
                    node_bytes, (4 * 512) - 14 - (2 * (i + 1))
                )  # for the last record this is the 'used' sentinel
                if record_start == 0xFFFF or record_end <= record_start:
                    continue
                record_bytes = node_bytes[record_start:record_end]
                if len(record_bytes) < 38 or record_bytes[0] != 0x24:
                    continue
                etype = (to_uint16_big_endian(record_bytes, 36) >> 8) & 0xFF
                if (
                    etype != 3
                ):  # Only FILEENTRY records have an s_file_id, and thus a hint sector to check:
                    continue
                num_files_found += 1
                name_bytes = record_bytes[3:35]
                null_index = name_bytes.find(b"\x00")
                key_name = (
                    name_bytes[:null_index] if null_index != -1 else name_bytes
                ).decode("cp1252", errors="replace")
                s_file_id = to_uint16_big_endian(record_bytes, 38)
                hint_sector_number, _ = self.get_sentry_for_sfile(s_file_id)
                if hint_sector_number is None:
                    print(
                        f"  WARNING: no hint sector found for file '{key_name}' (s_file_id={s_file_id:#06x}); cannot check its 'protected' flag."
                    )
                    continue
                hint_sector_number = self._locate_hint_page_for_sfile(
                    s_file_id, hint_sector_number
                )
                if hint_sector_number is None:
                    continue
                hint_sector_bytes = self.read_sector(hint_sector_number)
                if (
                    hint_sector_bytes[0x48] != 0
                ):  # The "protected" flag is set (see print_hint_sector_info()):
                    machine_id = to_uint32_big_endian(
                        hint_sector_bytes, 0x42
                    )  # 0x42: machine_id (the machine this file may be opened on)
                    # Per LISA_OS/OS/source-SERNUM.TEXT.unix.txt: machine_id = first3 * 65536 + last5, where
                    # first3/last5 are the BCD digits of the 8-digit AppleNet serial number.
                    applenet_id = f" = AppleNet '{machine_id // 65536:03d}{machine_id % 65536:05d}'"
                    print(
                        f"  - Found protected file named '{key_name}' with machine_id: {machine_id} ({machine_id:#010x}){applenet_id}, s_file_id {s_file_id}, hint_sector_number {hint_sector_number}"
                    )
                    protected_hint_sector_numbers.append(hint_sector_number)

                # self.pretty_print_tags_for_sector(hint_sector_number)
                # self.print_hint_sector_info(hint_sector_number)

            if next_page == 0xFFFFFFFF or next_page == 0 or next_page == current_page:
                break
            current_page = next_page

        print(
            f"Found {len(protected_hint_sector_numbers)} protected file(s) out of {num_files_found} total files."
        )
        return protected_hint_sector_numbers

    def get_sentry_for_sfile(self, s_file_id: int):
        """
        Look up the 14-byte s_entry record for an s-file number in the volume's s_entry table
        (the "slist"), and return (hint_sector_number, data_start_sector_number) as ABSOLUTE
        sector numbers; either element is None if it is unknown.

        Layout (see LISA_OS/OS/source-sfileio.text.unix.txt for s_entry, LISA_OS/OS/source-fsinit.text.unix.txt
        and LISA_OS/OS/source-ldlfs.text.unix.txt for its location and addressing):
          * The MDDF holds slist_addr (u32 @0x94: MDDF-relative page where the table starts),
            slist_packing (u16 @0x98: s_entries per page, usually 36) and
            slist_block_count (u16 @0x9A: number of pages the table spans).
          * s-file number N's record is at table page (N div slist_packing) and byte offset
            (N mod slist_packing) * 14 within it.
          * Each s_entry is: [hintaddr u32][fileaddr u32][filesize u32][version u16], big-endian.
            hintaddr = MDDF-relative page of the file's hint (hentry) sector, i.e. the sector
            decoded by print_hint_sector_info(); fileaddr = MDDF-relative page where the file's
            data begins. (A value of 0 or REDLIGHT=-1 means "none".)

        Note: the FILEENTRY catalog record itself does NOT contain the hint sector number;
        it only has the s_file_id, which is the index into this table. So it is being deriverd here.
        """
        if s_file_id <= 0:
            return None, None
        mddf_sector_bytes = self._mddf_sector_bytes
        slist_addr = to_uint32_big_endian(
            mddf_sector_bytes, 0x94
        )  # the MDDF-relative page number of the first slist sector (stored at offset 148 decimal in the MDDF)
        slist_packing = to_uint16_big_endian(
            mddf_sector_bytes, 0x98
        )  # the number of slist entries per sector (stored at offset 198 decimal in the MDDF); usually set to 36; 36*14 = 504, which fits well into the available 512 sector bytes
        slist_block_count = to_uint16_big_endian(
            mddf_sector_bytes, 0x9A
        )  # the number of slist sectors (stored at offset 154 decimal in the MDDF).
        if slist_packing == 0 or slist_block_count == 0:
            return None, None
        table_page_index = s_file_id // slist_packing
        if table_page_index >= slist_block_count:
            return None, None
        table_page = self.read_sector(
            self._mddf_sector_number + slist_addr + table_page_index
        )
        record_offset = (s_file_id % slist_packing) * 14
        hintaddr = to_uint32_big_endian(table_page, record_offset)
        fileaddr = to_uint32_big_endian(table_page, record_offset + 4)
        hint_sector_number = None
        if 0 < hintaddr < 0x7FFFFFFF:
            hint_sector_number = self._mddf_sector_number + hintaddr
            if hint_sector_number >= self._num_sectors:
                hint_sector_number = None
        data_start_sector_number = None
        if 0 < fileaddr < 0x7FFFFFFF:
            data_start_sector_number = self._mddf_sector_number + fileaddr
            if data_start_sector_number >= self._num_sectors:
                data_start_sector_number = None
        return hint_sector_number, data_start_sector_number

    def print_catalog_record(
        self, record: bytes, entry_number: int, rows: list | None = None
    ) -> str:
        """
        Print one catalog record found in a leaf node.

        Returns a category string: 'file', 'directory', 'thread' or 'other'.

        If rows is not None, file entries are not printed verbosely; instead their
        [s_file_id, file_name, size, created, last_modified] row is appended to rows
        (table mode, used by the 'list' command).

        All records start with a 36-byte key (see MakeKey in LISA_OS/OS/source-fsasm.text.unix.txt):
            [0x24][parent ID (2 bytes, big-endian)][name (up to 32 bytes, zero padded)][0x00]
        followed by a 2-byte eType field. In every .dc42 image examined, the entry type value is in
        the HIGH byte of that field (e.g. fileentry = 0x0300, threadentry = 0x08xx); the low byte
        holds extra per-entry information.

        The on-disk FILEENTRY is 64 bytes: key(36) + eType(2) + sfile(2) + fileDTC(4) + fileDTM(4)
        + size(4) + physSize(4) + fsOvrhd(2) + flags(2) + fileUnused(4). Everything is printed
        except the 4 reserved/unused bytes at offset 60. The file's hint sector number is NOT part
        of the FILEENTRY; it is looked up via the s_file_id in the volume's s_entry table
        (see get_sentry_for_sfile()).
        """
        if len(record) < 38 or record[0] != 0x24:
            print(
                f"  At catalog entry {entry_number}: unrecognized record ({len(record)} bytes):"
            )
            print_bytes_in_hex_and_ascii(record)
            return "other"

        parent_id = to_uint16_big_endian(record, 1)
        name_bytes = record[3:35]
        null_index = name_bytes.find(b"\x00")
        key_name = (name_bytes[:null_index] if null_index != -1 else name_bytes).decode(
            "cp1252", errors="replace"
        )

        etype_field = to_uint16_big_endian(record, 36)
        etype = (etype_field >> 8) & 0xFF
        etype_names = {
            0: "empty",
            1: "directory",
            2: "link",
            3: "file",
            4: "pipe",
            5: "ec",
            6: "killed",
            7: "removed",
            8: "thread",
        }

        if etype == 3:  # FILEENTRY, 64 bytes in total
            s_file_id = to_uint16_big_endian(record, 38)
            file_dtc = to_uint32_big_endian(record, 40)
            file_dtm = to_uint32_big_endian(record, 44)
            file_size = to_uint32_big_endian(record, 48)
            physical_size = to_uint32_big_endian(
                record, 52
            )  # The file_size, rounded up to the next 512 bytes. Typically the file_size is already rounded to that, so they match.
            fs_overhead = to_uint16_big_endian(record, 56)
            flags = to_uint16_big_endian(record, 58)
            file_unused = to_uint32_big_endian(
                record, 60
            )  # reserved for future use; printed for completeness
            if rows is not None:
                # Table mode (the 'list' command): collect this file's row for the
                # table printed by dump_catalog(); skip the verbose per-entry output
                # (and the s_entry lookup it needs).
                rows.append(
                    [
                        str(s_file_id),
                        key_name,
                        str(file_size),
                        format_date(file_dtc) if file_dtc != 0 else "never (0)",
                        format_date(file_dtm) if file_dtm != 0 else "never (0)",
                    ]
                )
                return "file"
            hint_sector_number, data_start_sector_number = self.get_sentry_for_sfile(
                s_file_id
            )
            hint_str = (
                f"hint sector {hint_sector_number} (rel page {hint_sector_number - self._mddf_sector_number})"
                if hint_sector_number is not None
                else "hint sector unknown (no s_entry found)"
            )
            data_start_str = (
                f"data start sector {data_start_sector_number} (rel page {data_start_sector_number - self._mddf_sector_number})"
                if data_start_sector_number is not None
                else "no data (empty file)"
            )
            print(
                f"  At catalog entry {entry_number}: file '{key_name}' (parent_id={parent_id}), "
                f"s_file_id={s_file_id:#06x}, file_size={file_size}, physical_size={physical_size}, fs_overhead={fs_overhead}, flags={flags:#06x}, file_unused={file_unused:#010x}"
            )
            print(f"      {hint_str}; {data_start_str}")
            print(
                f"      created: {format_date(file_dtc) if file_dtc != 0 else 'never (0)'}, modified: {format_date(file_dtm) if file_dtm != 0 else 'never (0)'}"
            )
            return "file"

        if etype == 1:  # DIRENTRY, 48 bytes in total
            dir_id = to_uint16_big_endian(record, 38)
            dir_dtc = to_uint32_big_endian(record, 40)
            print(
                f"  At catalog entry {entry_number}: directory '{key_name}' (parent_id={parent_id}), id={dir_id}, "
                f"created: {format_date(dir_dtc) if dir_dtc != 0 else 'never (0)'}"
            )
            return "directory"

        if (
            etype == 8
        ):  # THREADENTRY, 78 bytes in total: parID @ 38, myName @ 40 (a 34-byte Pascal string)
            par_id = to_uint16_big_endian(record, 38)
            name_len = min(record[40], 33) if len(record) > 41 else 0
            thread_name = record[41 : 41 + name_len].decode("cp1252", errors="replace")
            print(
                f"  At catalog entry {entry_number}: thread entry for '{key_name}' (parent_id={parent_id}), thread name='{thread_name}', parID={par_id}"
            )
            return "thread"

        # linkentry, pipeentry, ecentry, killedentry, removed, or something unknown:
        print(
            f"  At catalog entry {entry_number}: {etype_names.get(etype, f'unknown eType {etype}')} record '{key_name}' "
            f"(parent_id={parent_id}), eType field={etype_field:#06x}, {len(record)} bytes:"
        )
        print_bytes_in_hex_and_ascii(record)
        return "other"

    # ==================================================================
    # fs_version 14/15 (LOS 1.0 / LOS 2.0) support: the flat catalog
    # ==================================================================
    # Volumes with fs_version <= 15 (the "Flat_Catalog" case in the Lisa source,
    # see flat_catalog() in LISA_OS/OS/source-sfileio.text.unix.txt: fsversion <= PEPSI_VERSION,
    # where PEPSI_VERSION = 15) have NO B-tree catalog. Instead:
    #
    #  * The root catalog is a regular file (the sfile whose hentry ftype is rootcat=2;
    #    on such volumes it is the sfile number `first_file` from the MDDF) whose data is
    #    a fixed array of 54-byte "centry" records (rootmaxentries of them, which equals
    #    maxfiles): a hashed table with linear probing.
    #
    #  * centry layout (see the centry record and MAKE_ENTRY/KILL_ENTRY/LOOKUP_BY_ENAME in
    #    LISA_OS/OS/source-fsprim.text.unix.txt; note that in this version enums are stored
    #    as 1 byte on disk, which is why the record is 34+1+1+2+4+4+2+4+2 = 54 bytes):
    #        name       : e_name;      (* 34 bytes: 1 length byte + 33 chars *)
    #        cetype     : entrytype;   (* 1 byte: 0=emptyentry, 1=direntry, 2=linkentry,
    #                                       3=fileentry, 4=pipeentry, 5=ecentry,
    #                                       6=killedentry, 7=removed, 8=threadentry *)
    #        (1 pad byte)
    #        sfile      : integer;
    #        attributes : longint;     (* reserved for future use *)
    #        readpage   : longint;  readoffset : integer;   (* pipe entries only *)
    #        writepage  : longint;  writeoffset: integer;   (* pipe entries only *)
    #
    #    The slot number of an entry is hash(uppercase(name), rootmaxentries), where the
    #    hash is (see LOOKUP_BY_ENAME in source-fsprim.text.unix.txt):
    #        temp = ord(c1)*(ord(clast)+1); for m = l-2 downto 1: temp += ord(c_{m+1})*(ord(c_{m+2})+1);
    #        temp = abs(temp) mod rootmaxentries;
    #    with linear probing for collisions. The rootcatalog itself has no catalog entry.
    #
    #  * The per-file hint sector (hentry) has the same field layout as fs_version 16/17,
    #    except that e_name is 34 bytes (string[33]) and the hentry is only 88 bytes long
    #    (the build_info..fsOverhead tail fields don't exist; UpgradeFile in
    #    source-fsprim.text.unix.txt zeroes the 40 bytes after the 88-byte hentry when
    #    upgrading such a volume). Layout: name(34)@0x00, UID(8)@0x22, version(2)@0x2A,
    #    ftype(1)@0x2C, unknown(1)@0x2D, DTC(4)@0x2E, DTA(4)@0x32, DTM(4)@0x36, DTB(4)@0x3A,
    #    DTS(4)@0x3E, machine_id(4)@0x42, 7 boolean flags @0x46..0x4C (killed, safety_on,
    #    protected, master, scavenged, closed_by_OS, file_open), then 2-byte integers
    #    (result_scavenge@0x4D, unusedi1@0x4F, system_type@0x51, user_type@0x53,
    #    user_subtype@0x55) through 0x57.
    #
    #  * File data is read exactly as for fs_version 16/17: start at the slist's fileaddr
    #    (MDDF-relative), and follow the 20-byte tag fwd_links (also MDDF-relative,
    #    0xFFFFFF = end). In 20-byte tags the dataused field has the 0x8000 flag bit set
    #    on every tag, so the valid byte count per sector is dataused & 0x7FFF. Stop after
    #    the slist's filesize bytes. A file with fileaddr=0 or filesize=0 is empty.
    def is_flat_catalog_volume(self) -> bool:
        """True if this volume uses a flat (no-B-tree) catalog, i.e. fs_version 14/15 (LOS 1.0/2.0)."""
        return 0 < self._fs_version <= 15

    def _slist_entry(self, s_file_id: int) -> tuple[int, int, int, int] | None:
        """Read the 14-byte s_entry record for the given s_file_id from the volume's slist.

        Returns (hintaddr, fileaddr, filesize, version) as MDDF-relative values
        (0 = "none"), or None if the slist doesn't cover this s_file_id.
        (See get_sentry_for_sfile() for the addressing scheme; this variant also returns
        the filesize and the raw MDDF-relative values.)
        """
        slist_addr = to_uint32_big_endian(self._mddf_sector_bytes, 0x94)
        slist_packing = to_uint16_big_endian(self._mddf_sector_bytes, 0x98)
        slist_block_count = to_uint16_big_endian(self._mddf_sector_bytes, 0x9A)
        if s_file_id <= 0 or slist_packing == 0 or slist_block_count == 0:
            return None
        table_page_index = s_file_id // slist_packing
        if table_page_index >= slist_block_count:
            return None
        table_page = self.read_sector(
            self._mddf_sector_number + slist_addr + table_page_index
        )
        record_offset = (s_file_id % slist_packing) * 14
        hintaddr = to_uint32_big_endian(table_page, record_offset)
        fileaddr = to_uint32_big_endian(table_page, record_offset + 4)
        filesize = to_uint32_big_endian(table_page, record_offset + 8)
        version = to_uint16_big_endian(table_page, record_offset + 12)
        return (hintaddr, fileaddr, filesize, version)

    def _flat_catalog_sfile_range(self):
        """Returns (first_sfile, last_sfile) to scan: all sfiles that may hold a file.
        On flat-catalog volumes the MDDF's first_file points at the rootcatalog itself,
        so we scan from sfile 1 to empty_file-1 and skip the unused (hintaddr=0) slots.
        """
        mddf_sector_bytes = self._mddf_sector_bytes
        empty_file = to_uint16_big_endian(mddf_sector_bytes, 158)
        return (1, empty_file - 1)

    def _locate_hint_page_for_sfile(self, s_file_id: int, hint_sector_number: int):
        """Verify that the hint_sector_number really is the hint page of the given s-file, by looking at  tag data.

        If it is, just return the passed hint_sector_number.
        If it is not, locate the real hint page by searching through all sector tags.

        A file's hint page is always tagged with fileid = -s_file_id: NEW_SFILE allocates
        the hint pages with a negative fileid (appendpages(..., -free, ...) in
        LISA_OS/OS/source-sfileio1.text.unix.txt) and stores the first page's address as
        the slist entry's hintaddr. Data pages of the same file are tagged +s_file_id,
        and free pages are tagged 0.

        If the sector at the slist's hintaddr does not carry the -s_file_id tag, the slist
        entry is stale: it points at a sector that is no longer (or was never) this file's
        hint page - e.g. a data page of some other file or a free page. Reading such a
        sector as an hentry yields a garbage file name and a random 'protected' flag.
        In that case, scan all sector tags for the real hint page (fileid = -s_file_id,
        relpage = 0) and use it instead.

        Returns the hint sector number (absolute), or None if no valid hint page exists.
        """
        expected_file_id_in_tag_data = (0x10000 - s_file_id) % 0x10000
        if (
            self.get_file_id_from_tag_data_for_sector(hint_sector_number)
            == expected_file_id_in_tag_data
        ):
            return hint_sector_number
        stale_file_id = self.get_file_id_from_tag_data_for_sector(hint_sector_number)

        # NOTE: The "WARNING" below is being printed for Twiggy floppy image file LOS1.01.dc42 .
        # It is possible that I have a bug in the caller code that is deriving the
        # hint_sector_number wrongly.
        print(
            f"  WARNING: stale slist entry for s_file_id={s_file_id}: sector {hint_sector_number} (its hintaddr) "
            f"is tagged file_id={stale_file_id:#06x}, but a hint page must be tagged -s_file_id ({expected_file_id_in_tag_data:#06x}). "
            f"The slist points at a sector that is not this file's hint page; scanning all sector tags for the real one ..."
        )
        rel_offset = 6 if self._single_tag_size == 12 else 12
        for sector_number in range(0, self._num_sectors):
            tag_bytes = self.read_tags_for_sector(sector_number)
            if struct.unpack(">H", tag_bytes[4:6])[0] != expected_file_id_in_tag_data:
                continue
            if struct.unpack(">H", tag_bytes[rel_offset : rel_offset + 2])[0] != 0:
                continue  # the hentry lives in relpage 0 of the hint pages
            print(
                f"  Found the real hint page for s_file_id={s_file_id} at sector {sector_number}."
            )
            return sector_number
        print(
            f"  WARNING: no hint page tagged -s_file_id ({expected_file_id_in_tag_data:#06x}) exists on this volume for s_file_id={s_file_id}; skipping it."
        )
        return None

    def flat_catalog_read_file_data(self, s_file_id: int) -> bytes:
        """Read the full contents of the given sfile by following the tag chain that
        starts at its data start sector (the slist's fileaddr, MDDF-relative).

        For each sector, only the first `dataused` bytes are valid. The chain ends at
        the slist's filesize bytes or a fwd_link of the end-of-chain sentinel
        (0xFFFFFF in 20-byte tags, 0x07FF in 12-byte tags).
        A file with fileaddr=0 or filesize=0 yields an empty result.
        """
        entry = self._slist_entry(s_file_id)
        if entry is None:
            raise ValueError(f"No slist entry for s_file_id {s_file_id}")
        hintaddr, fileaddr, filesize, version = entry
        if fileaddr == 0 or filesize == 0:
            return b""
        if self._single_tag_size not in (12, 20):
            raise ValueError(
                f"flat_catalog_read_file_data supports 12-byte (floppy) and 20-byte (hard disk) tags, but this image has {self._single_tag_size}-byte tags."
            )
        out = bytearray()
        sector = self._mddf_sector_number + fileaddr
        iterations = 0
        while len(out) < filesize:
            if sector >= self._num_sectors:
                raise ValueError(
                    f"Tag chain for sfile {s_file_id} points beyond the last sector (sector {sector} of {self._num_sectors})."
                )
            if iterations > 20000:
                raise ValueError(
                    f"Possible infinite loop while reading file data of sfile {s_file_id}! Perhaps the file system is corrupted? Exiting."
                )
            iterations += 1
            sector_bytes = self.read_sector(sector)
            tag = self.read_tags_for_sector(sector)
            dataused, fwd_link, end_sentinel = self._decode_tag_dataused_and_fwd_link(
                tag
            )
            out += sector_bytes[:dataused]
            if len(out) >= filesize or fwd_link == end_sentinel:
                break
            sector = self._mddf_sector_number + fwd_link
        # Fallbacks for stale slist entries / broken chains: on some images (e.g. LOS1.01.dc42)
        # the slist's fileaddr points at sectors that no longer belong to the file, and/or the
        # tags' fwd_link chain is broken, so the chain walk above yields the wrong (or too little)
        # data. The per-sector file_id in the tag is the most reliable record of which sectors
        # belong to the file, so in that case read the sectors tagged +s_file_id instead:
        data_start_sector = self._mddf_sector_number + fileaddr
        if data_start_sector < self._num_sectors:
            data_start_file_id = self.get_file_id_from_tag_data_for_sector(
                data_start_sector
            )
            if data_start_file_id != s_file_id:
                # Stale slist entry: the chain walk above read the data of whatever file now
                # owns the slist's fileaddr sector - untrustworthy. Prefer this file's own sectors:
                tagged = self._read_file_data_from_tagged_sectors(s_file_id, filesize)
                if tagged:
                    print(
                        f"  NOTE: stale slist entry for s_file_id={s_file_id}: its fileaddr points at sector "
                        f"{data_start_sector} (tagged +{data_start_file_id:#06x}, not +{s_file_id:#06x}); "
                        f"recovered {len(tagged)} of {filesize} bytes by reading all sectors tagged +{s_file_id:#06x} in sector-number order."
                    )
                    out = tagged
            elif min(len(out), filesize) < filesize:
                # The data start sector is correct, but the fwd_link chain broke early:
                tagged = self._read_file_data_from_tagged_sectors(s_file_id, filesize)
                if len(tagged) > len(out):
                    print(
                        f"  NOTE: the fwd_link chain of s_file_id={s_file_id} broke after {min(len(out), filesize)} of {filesize} bytes; "
                        f"recovered {len(tagged)} bytes by reading all sectors tagged +{s_file_id:#06x} in sector-number order."
                    )
                    out = tagged
        return bytes(out[:filesize])

    def _read_file_data_from_tagged_sectors(
        self, s_file_id: int, filesize: int
    ) -> bytes:
        """Read a file's data from every sector whose tag carries this file's file_id (+s_file_id),
        in ascending sector-number order, truncated to `filesize`.

        Fallback for stale slist entries / broken fwd_link chains, where the normal chain walk
        (starting at the slist's fileaddr) yields the wrong data: the per-sector file_id in the
        tag is the most reliable record of which sectors belong to the file. Each sector
        contributes its `dataused` bytes.
        """
        out = bytearray()
        for sector in range(0, self._num_sectors):
            if self.get_file_id_from_tag_data_for_sector(sector) != s_file_id:
                continue
            tag = self.read_tags_for_sector(sector)
            dataused, _, _ = self._decode_tag_dataused_and_fwd_link(tag)
            out += self.read_sector(sector)[:dataused]
            if len(out) >= filesize:
                break
        return bytes(out[:filesize])

    def _decode_tag_dataused_and_fwd_link(self, tag: bytes):
        """Decode (dataused, fwd_link, end_sentinel) from a sector's tag.

        dataused     = the number of valid bytes in the sector's 512-byte data field.
        fwd_link     = the MDDF-relative next data sector in the file's page chain.
        end_sentinel = the fwd_link value that marks the end of the chain.

        20-byte tags (hard disk images):
            dataused = bytes 6-7; a 0x8000 flag bit is set on every tag, so mask it off.
            fwd_link = 3 bytes at 0x0E..0x10; end of chain = 0xFFFFFF.

        12-byte tags (floppy images): there is no separate dataused field and no
        checksum. Instead the two 11-bit links are packed together with dataused, which
        is split across both words (see FINISH_READ in SOURCE-SONYASM.TEXT.unix.txt):
            bytes 8-9   = dataused_hi (top 5 bits) | fwd_link (low 11 bits)
            bytes 10-11 = dataused_lo (top 5 bits) | bkwd_link (low 11 bits)
            dataused = (dataused_hi << 5) | dataused_lo   (a 10-bit value, 0..512)
            fwd_link = the low 11 bits; end of chain = 0x07FF.
        """
        if self._single_tag_size == 20:
            dataused = (
                to_uint16_big_endian(tag, 6) & 0x7FFF
            )  # mask off the 0x8000 flag bit
            fwd_link = (
                (tag[0x0E] << 16) | (tag[0x0F] << 8) | tag[0x10]
            )  # 3-byte MDDF-relative link
            return dataused, fwd_link, 0xFFFFFF
        if self._single_tag_size == 12:
            w89 = (tag[8] << 8) | tag[
                9
            ]  # dataused_hi (top 5 bits) | fwd_link (low 11 bits)
            w1011 = (tag[10] << 8) | tag[
                11
            ]  # dataused_lo (top 5 bits) | bkwd_link (low 11 bits)
            fwd_link = w89 & 0x07FF  # 11-bit MDDF-relative link
            dataused = ((w89 >> 11) << 5) | (
                w1011 >> 11
            )  # reassemble the 10-bit dataused
            return dataused, fwd_link, 0x07FF
        raise ValueError(
            f"Unsupported tag size {self._single_tag_size} in _decode_tag_dataused_and_fwd_link."
        )

    def _find_rootcatalog_sfile(self):
        """Find the s_file_id of the root catalog: the sfile whose hentry ftype is rootcat (2).
        On flat-catalog volumes this is the sfile number `first_file` from the MDDF, but we
        locate it by scanning the slist, which is more robust."""
        first_sfile, last_sfile = self._flat_catalog_sfile_range()
        for s_file_id in range(first_sfile, last_sfile + 1):
            entry = self._slist_entry(s_file_id)
            if entry is None:
                continue
            hintaddr, fileaddr, filesize, version = entry
            if hintaddr == 0:
                continue
            hint_bytes = self.read_sector(self._mddf_sector_number + hintaddr)
            if hint_bytes[0x2C] == 2:  # ftype = rootcat (see FILETYPE_NAMES)
                return s_file_id
        raise ValueError(
            "Could not find the rootcatalog sfile (no hentry with ftype=rootcat=2 in the slist)."
        )

    def flat_catalog_list_files(self):
        """List all files on this flat-catalog (fs_version 14/15) volume, using the slist
        and the hint (hentry) sectors. No B-tree is involved."""
        first_sfile, last_sfile = self._flat_catalog_sfile_range()
        print(
            f"\nFlat catalog (fs_version {self._fs_version}): no B-tree catalog on this volume; listing files from the slist (sfile {first_sfile}..{last_sfile}) ..."
        )
        print(
            f"Note: MDDF says filecount={to_uint16_big_endian(self._mddf_sector_bytes, 176)}, empty_file={to_uint16_big_endian(self._mddf_sector_bytes, 158)}, maxfiles={to_uint16_big_endian(self._mddf_sector_bytes, 160)}."
        )
        num_files = 0
        rows: list[list[str]] = []
        for s_file_id in range(first_sfile, last_sfile + 1):
            entry = self._slist_entry(s_file_id)
            if entry is None:
                continue
            hintaddr, fileaddr, filesize, _ = entry
            if hintaddr == 0:
                continue  # unused slist slot
            num_files += 1
            hint_sector_number = self._mddf_sector_number + hintaddr
            # The slist's hintaddr can be stale (it may point at a sector that is not this
            # file's hint page, e.g. a data page of another file or a free page); in that
            # case reading it as an hentry yields a garbled name and a bogus ftype.
            # Verify/relocate the hint page via its tag (fileid = -s_file_id):
            hint_sector_number = self._locate_hint_page_for_sfile(
                s_file_id, hint_sector_number
            )
            if hint_sector_number is None:
                continue  # no valid hint page exists for this sfile
            hint_sector_bytes = self.read_sector(hint_sector_number)
            file_name = pascal_to_string(hint_sector_bytes, start=0)
            # The hentry's DTC/DTM fields (created / last modified; layout above,
            # DTC(4)@0x2E, DTM(4)@0x36)
            file_dtc = to_uint32_big_endian(hint_sector_bytes, 0x2E)
            file_dtm = to_uint32_big_endian(hint_sector_bytes, 0x36)
            rows.append(
                [
                    str(s_file_id),
                    file_name,
                    str(filesize),
                    format_date(file_dtc) if file_dtc != 0 else "never (0)",
                    format_date(file_dtm) if file_dtm != 0 else "never (0)",
                ]
            )
        if rows:
            print()
            for line in format_table(
                ["s_file_id", "file_name", "size", "created", "last_modified"],
                rows,
            ):
                print(line)
        print(f"Found {num_files} file(s) in the slist.")

    def flat_catalog_dump_catalog(self):
        """Dump the flat (hashed) catalog of this fs_version 14/15 volume: it is the data of
        the rootcatalog file, an array of 54-byte centry records (see the comment above).
        Cross-checks each entry against the slist and verifies the hash slot placement.
        """
        rootmaxentries = to_uint16_big_endian(self._mddf_sector_bytes, 192)
        maxfiles = to_uint16_big_endian(self._mddf_sector_bytes, 160)
        try:
            rootcat_sfile = self._find_rootcatalog_sfile()
        except ValueError as e:
            print(f"\n{e}")
            return
        print(
            f"\nDumping the flat catalog (fs_version {self._fs_version}): rootcatalog sfile {rootcat_sfile}, "
            f"rootmaxentries={rootmaxentries}, maxfiles={maxfiles}"
        )
        data = self.flat_catalog_read_file_data(rootcat_sfile)
        num_slots = len(data) // 54
        if num_slots != rootmaxentries:
            print(
                f"WARNING: the rootcatalog data has {len(data)} bytes = {num_slots} x 54-byte records, but the MDDF says rootmaxentries={rootmaxentries}. Continuing with {num_slots} slots."
            )
        cetype_names = {
            0: "emptyentry",
            1: "direntry",
            2: "linkentry",
            3: "fileentry",
            4: "pipeentry",
            5: "ecentry",
            6: "killedentry",
            7: "removed",
            8: "threadentry",
        }
        counts = {}
        num_hash_ok = 0
        for slot in range(num_slots):
            rec = data[slot * 54 : slot * 54 + 54]
            name_len = rec[0]
            name = (
                rec[1 : 1 + name_len].decode("mac-roman", errors="replace")
                if name_len > 0
                else ""
            )
            cetype = rec[34]
            if cetype == 0 and name == "":
                continue  # completely empty slot
            counts[cetype] = counts.get(cetype, 0) + 1
            sfile = to_uint16_big_endian(rec, 36)
            attributes = to_uint32_big_endian(rec, 38)
            readpage = to_uint32_big_endian(rec, 42)
            readoffset = to_uint16_big_endian(rec, 46)
            writepage = to_uint32_big_endian(rec, 48)
            writeoffset = to_uint16_big_endian(rec, 52)
            hash_slot = flat_catalog_hash(name, rootmaxentries) if name else -1
            hash_note = ""
            if hash_slot != -1:
                if hash_slot == slot:
                    num_hash_ok += 1
                else:
                    hash_note = (
                        f"  (hash={hash_slot}, probed {slot - hash_slot} slot(s))"
                        if slot > hash_slot
                        else f"  (hash={hash_slot}: BEFORE current slot - unexpected!)"
                    )
            slist_note = ""
            if cetype == 3:  # fileentry: cross-check the sfile against the slist
                entry = self._slist_entry(sfile)
                if entry is None:
                    slist_note = "  (no slist entry for this sfile!)"
                else:
                    hintaddr, fileaddr, filesize, version = entry
                    if hintaddr != 0:
                        hint_bytes = self.read_sector(
                            self._mddf_sector_number + hintaddr
                        )
                        hint_name = pascal_to_string(hint_bytes, start=0)
                        if hint_name != name:
                            slist_note = f"  (slist hint name mismatch!)"  # don't print the hint_name, it is garbage
            print(
                f"  slot {slot:5}: '{name}': cetype={cetype_names.get(cetype, f'unknown {cetype}')} ({cetype}), sfile={sfile}, "
                f"attributes={attributes:#010x}, readpage={readpage} (off {readoffset}), writepage={writepage} (off {writeoffset}){hash_note}{slist_note}"
            )
        total_non_empty = sum(counts.values())
        summary = ", ".join(
            f"{cetype_names.get(k, k)}={v}" for k, v in sorted(counts.items())
        )
        print(f"Total: {total_non_empty} non-empty slot(s): {summary}")

    def _find_protected_files_flat_catalog(self) -> list:
        """fs_version 14/15 variant of find_protected_files(): no B-tree catalog, so scan
        the slist and check byte 0x48 ('protected' flag) of each file's hint sector."""
        protected_hint_sector_numbers: list = []
        first_sfile, last_sfile = self._flat_catalog_sfile_range()
        num_files_found = 0
        for s_file_id in range(first_sfile, last_sfile + 1):
            entry = self._slist_entry(s_file_id)
            if entry is None:
                continue
            hintaddr, fileaddr, filesize, version = entry
            if hintaddr == 0:
                continue
            num_files_found += 1
            hint_sector_number = self._mddf_sector_number + hintaddr
            hint_sector_number = self._locate_hint_page_for_sfile(
                s_file_id, hint_sector_number
            )
            if hint_sector_number is None:
                continue
            hint_sector_bytes = self.read_sector(hint_sector_number)
            key_name = pascal_to_string(hint_sector_bytes, start=0)
            if (
                hint_sector_bytes[0x48] != 0
            ):  # The "protected" flag is set (see print_hint_sector_info()):
                machine_id = to_uint32_big_endian(
                    hint_sector_bytes, 0x42
                )  # 0x42: machine_id (the machine this file may be opened on)
                # Per LISA_OS/OS/source-SERNUM.TEXT.unix.txt: machine_id = first3 * 65536 + last5, where
                # first3/last5 are the BCD digits of the 8-digit AppleNet serial number.
                applenet_id = (
                    f" = AppleNet '{machine_id // 65536:03d}{machine_id % 65536:05d}'"
                )
                print(
                    f"  - Found protected file named '{key_name}' with machine_id: {machine_id} ({machine_id:#010x}){applenet_id}, s_file_id {s_file_id}, hint_sector_number {hint_sector_number}"
                )
                protected_hint_sector_numbers.append(hint_sector_number)
        print(
            f"Found {len(protected_hint_sector_numbers)} protected file(s) out of {num_files_found} total files."
        )
        return protected_hint_sector_numbers

    def _dump_files_flat_catalog(self, flatten: bool = False):
        """fs_version 14/15 variant of dump_files(): write the contents of every file in the
        slist to folder /tmp/LisaFileSystemDump, following each file's tag chain (see
        flat_catalog_read_file_data()). The rootcatalog itself is not dumped as a file.
        By default a '/' in a file name becomes a subfolder; with flatten=True it is
        replaced with '-' (see dump_files() and _dump_destination()).
        Like dump_files() above, ".TEXT" files are dumped as plain host text:
        the 1024-byte header page and the null page padding are stripped, and
        the CR line endings are converted to host "\n" (see
        lisa_text_file_to_host_text()).
        """
        output_dir = "/tmp/LisaFileSystemDump"
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
            print(f"Created directory: {output_dir}")
        first_sfile, last_sfile = self._flat_catalog_sfile_range()
        num_files_written = 0
        for s_file_id in range(first_sfile, last_sfile + 1):
            entry = self._slist_entry(s_file_id)
            if entry is None:
                continue
            hintaddr, fileaddr, filesize, version = entry
            if hintaddr == 0:
                continue
            # The slist's hintaddr may be stale: it can point at a sector that no longer belongs
            # to this file (e.g. one that was freed and reused). Reading such a sector as an
            # hentry yields a garbage file name, often with characters that are invalid in Linux
            # file names, which would crash the open() below. Locate the real hint page instead,
            # exactly like the 'list' command does (it prints a 'stale slist entry' warning):
            hint_sector_number = self._locate_hint_page_for_sfile(
                s_file_id, self._mddf_sector_number + hintaddr
            )
            if hint_sector_number is None:
                continue
            hint_bytes = self.read_sector(hint_sector_number)
            file_name = pascal_to_string(hint_bytes, start=0)
            if hint_bytes[0x2C] == 2:  # skip the rootcatalog itself (ftype = rootcat)
                print(
                    f"Skipping the rootcatalog: s_file_id={s_file_id}, file_name='{file_name}'"
                )
                continue
            if file_name == "":
                print(
                    f"Skipping this empty file name: s_file_id={s_file_id}, file_name='{file_name}'"
                )
                continue
            if "\x00" in file_name:
                # The file name (from a stale/corrupt hentry) contains a
                # character that cannot appear in a Linux file name (a null
                # byte): skip it rather than crash. (A '/' in the name is fine:
                # Lisa does not treat it as a folder delimiter, and when saving
                # to the host we use it as one, writing the file into a
                # matching subfolder of the dump folder.)
                print(
                    f"Skipping file with an invalid file name {file_name!r}: s_file_id={s_file_id}"
                )
                continue
            file_data = self.flat_catalog_read_file_data(s_file_id)
            if len(file_data) != filesize:
                print(
                    f"WARNING: read {len(file_data)} bytes for file '{file_name}' (s_file_id={s_file_id}), but the slist says its size is {filesize} bytes!"
                )
            # Lisa OS text files are special: the on-disk layout is a 1024-byte header
            # page followed by 1024-byte pages of CR-terminated lines, null-padded
            # after the last line of each page. Dump them as plain host text, like
            # dump_files() above: the inverse of build_lisa_text_file_data() from
            # LisaFileSystemToolAddFile.py — strip the header page and the null page
            # padding, and convert the Lisa CR line endings to host "\n".
            if file_name.upper().endswith(".TEXT"):
                file_data = lisa_text_file_to_host_text(file_data)
            # Skip any name that would escape the dump folder (a leading '/' or
            # '..' components); in flatten mode a '/' in the name becomes a '-'.
            output_filename = self._dump_destination(file_name, flatten)
            if output_filename is None:
                print(
                    f"Skipping file with an unsafe file name {file_name!r}: s_file_id={s_file_id}"
                )
                continue
            # Ensure the output directory exists (file names containing '/' are
            # saved into matching subfolders).
            parent_dir = os.path.dirname(output_filename)
            if not os.path.exists(parent_dir):
                os.makedirs(parent_dir)
                print(f"Created directory: {parent_dir}")
            with open(output_filename, "wb") as output_file_handler:
                output_file_handler.write(file_data)
            num_files_written += 1
            print(
                f"Wrote '{file_name}' (s_file_id={s_file_id}, {len(file_data)} bytes) to {output_filename}"
            )
        print(f"Done: wrote {num_files_written} file(s) to {output_dir}")

    def find_all_sector_numbers_with_given_file_id_type(self, file_id_type: int) -> int:
        """Scans all tags, looking for one with file id type of e.g. 0x0001 (the MDDF sector type)

        Returns a list of zero-based sector numbers where such tags are found.
        Returns an empty list if no matching tags are found.
        """
        found_sector_numbers: List[int] = []
        for i in range(0, self._num_sectors):
            this_tag_file_id_type = self.get_file_id_from_tag_data_for_sector(i)
            if this_tag_file_id_type == file_id_type:
                found_sector_numbers.append(i)
        return found_sector_numbers


def flat_catalog_hash(name: str, maxindex: int) -> int:
    """The catalog hash used by LOOKUP_BY_ENAME in LISA_OS/OS/source-fsprim.text.unix.txt:
    the entry name is uppercased, then
        temp = ord(c1) * (ord(clast) + 1)
        for m = l-2 downto 1: temp += ord(c_{m+1}) * (ord(c_{m+2}) + 1)
        result = abs(temp) mod maxindex
    (Pascal strings are 1-based; the loop is rewritten 0-based here.)
    """
    s = name.upper()
    l = len(s)
    if l <= 0:
        return 0
    temp = ord(s[0]) * (ord(s[l - 1]) + 1)
    for j in range(l - 3, -1, -1):
        temp += ord(s[j + 1]) * (ord(s[j + 2]) + 1)
    if temp < 0:
        temp = -temp
    return temp % maxindex


def pascal_to_string(pascal_string: bytes, encoding="mac-roman", start: int = 0) -> str:
    """Converts a Pascal-style string (length-prefixed) to a Python string.

    Args:
        pascal_string (bytes): The byte array containing the Pascal string.
        encoding (str): The encoding to use for decoding (default is 'mac-roman').
        start (int): The starting index of the Pascal string in the byte array.

    Returns:
        str: The decoded string.
    """
    length = pascal_string[start]  # Read the length byte
    actual_length = 0

    for i in range(start + 1, start + 1 + length):
        if i >= len(pascal_string) or pascal_string[i] == 0:
            break
        actual_length += 1

    dest = pascal_string[
        start + 1 : start + 1 + actual_length
    ]  # Extract the string bytes
    return dest.decode(encoding) if actual_length > 0 else ""


def format_table(
    headers: list[str], rows: list[list[str]], max_width: int = 80, flex_col: int = 1
) -> list[str]:
    """Format column headers and rows as a fixed-width table of at most max_width
    characters.

    Every column is as wide as its widest cell, except column flex_col (the file
    name column), which absorbs whatever width is left in the max_width budget;
    a cell that does not fit is truncated with '...' at the end.
    """
    num_cols = len(headers)
    widths = [len(header) for header in headers]
    for row in rows:
        for i in range(num_cols):
            widths[i] = max(widths[i], len(row[i]))
    sep = "  "
    # Give column flex_col whatever budget is left after the other columns:
    widths[flex_col] = min(
        widths[flex_col],
        max(
            max_width - (sum(widths) - widths[flex_col]) - len(sep) * (num_cols - 1), 1
        ),
    )

    def truncate(cell: str, width: int) -> str:
        if len(cell) <= width:
            return cell
        return (cell[: width - 3] + "...") if width > 3 else cell[:width]

    lines = [
        sep.join(
            truncate(header, width).ljust(width)
            for header, width in zip(headers, widths)
        ).rstrip(),
        "-" * (sum(widths) + len(sep) * (num_cols - 1)),
    ]
    for row in rows:
        lines.append(
            sep.join(
                truncate(cell, width).ljust(width) for cell, width in zip(row, widths)
            )
        )
    return lines


def _expand_dle_leading_spaces(line: bytes) -> bytes:
    """Expand the optional leading-space compression code of one line.

    Per LisaOsTextFileSpecification.txt, a sequence of spaces at the beginning
    of a line may be stored as a two-byte code: a DLE character (0x10) followed
    by a byte containing 32 plus the number of spaces represented. Real Lisa
    text editors write this code; a DLE at the start of a line is always
    interpreted as the code by Lisa readers, so it is expanded here (the code
    never appears anywhere else in a line). A DLE followed by a byte below 33
    (i.e. "zero or fewer" spaces, a degenerate code no writer emits) is not
    expanded, so a literal 0x10 0x20 pair at a line start is preserved.
    """
    if len(line) >= 2 and line[0] == 0x10 and line[1] >= 33:
        return b" " * (line[1] - 32) + line[2:]
    return line


def lisa_text_file_to_host_text(file_data: bytes) -> bytes:
    """The inverse of LisaFileSystemToolAddFile.build_lisa_text_file_data():
    convert the on-disk byte stream of a Lisa ".TEXT" file into plain host text.

    The on-disk layout (see LisaOsTextFileSpecification.txt) is a 1024-byte
    header page (all-null for files written by this tool, but real Lisa text
    editors store formatting data there) followed by 1024-byte text pages;
    each page contains
    CR-terminated (0x0D) lines and is filled with nulls after the last line,
    the first CR-null (0x0D 0x00) pair in a page signaling its end. This
    function strips the header page and the null padding of every page,
    expands the optional DLE leading-space compression code (see
    _expand_dle_leading_spaces()), and converts the Lisa CR line endings to
    host "\\n".

    Notes:
      * a page may end well before its 1023rd byte: real Lisa text editors
        keep historical page boundaries, so the first CR-null pair can appear
        at any position (it is always honored, as it is by Lisa readers);
      * a page without a CR-null terminator (malformed) contributes its bytes
        with the trailing nulls removed;
      * a line longer than 1023 bytes was split across pages by the writer,
        with a CR inserted at each page boundary (see
        build_lisa_text_file_data()); those inserted CRs are indistinguishable
        from real line ends, so they appear as extra "\\n" in the result;
      * a null byte in the middle of a line (not after a CR) is kept as-is;
      * this is an exact byte-level inverse for files that do not use the
        DLE leading-space compression; for files that do (real Lisa editor
        files), the result holds the same logical text, with the compressed
        spaces expanded to literal space characters.
    """
    page_size = 2 * SECTOR_SIZE_IN_BYTES
    # Strip the header page (not part of the file's contents). A ".TEXT" file
    # is always at least one page long; a shorter (malformed) file is kept whole.
    if len(file_data) >= page_size:
        file_data = file_data[page_size:]
    out = bytearray()
    for off in range(0, len(file_data), page_size):
        page = file_data[off : off + page_size]
        pos = page.find(b"\r\x00")
        if pos >= 0:
            # The page's contents end at the first CR of the CR-null terminator:
            out += page[: pos + 1]
        else:
            # Malformed page (no CR-null terminator): keep the data, drop the
            # trailing null padding.
            out += page.rstrip(b"\x00")
    # Expand the DLE leading-space compression code at the start of each
    # (CR-terminated) line; the code can only occur there:
    out = b"\r".join(
        _expand_dle_leading_spaces(line) for line in bytes(out).split(b"\r")
    )
    return out.replace(b"\r", b"\n")


def compute_dc42_checksum_of_file_data_block(
    file: BufferedReader, data_start: int, data_length: int
) -> int:
    if (
        data_length > 1000000000
    ):  # Sanity check. Ideally we should check if data_length goes beyond the max file size
        return -1
    # The data starts at offset 0x54:
    file.seek(data_start)
    file_data_bytes = file.read(data_length)
    return compute_dc42_checksum(file_data_bytes)


def compute_dc42_checksum(data_bytes: bytearray) -> int:
    """Compute checksum DC42 uses to verify sector and tag data integrity.

    Shamelessly copied from https://github.com/stepleton/bootloader/blob/master/dc42_build_bootable_disk.py

    Args:
        data: data to compute a checksum for.

    Returns: a 32-bit (big endian) checksum.
    """

    def addl_rorl(uint, csum):
        """Add `uint` to `csum`; 32-bit truncate; 32-bit rotate right one bit."""
        csum += uint  # add uint
        csum &= 0xFFFFFFFF  # truncate
        rbit = csum & 0x1  # rotate part 1 (save low-order bit)
        csum >>= 1  # rotate part 2 (shift right)
        csum += rbit << 31  # rotate part 3 (prepend old low-order bit)
        return csum

    # Loop over all two-byte words in the data and include them in the checksum.
    checksum = 0
    for word_bytes in [data_bytes[i : i + 2] for i in range(0, len(data_bytes), 2)]:
        word = struct.unpack(">H", word_bytes)[0]  # big endian word bytes to native
        checksum = addl_rorl(word, checksum)  # add to checksum

    # return result as a big-endian 32-bit word.
    return checksum  # struct.pack('>I', checksum)


def disk_type_to_string(disk_type: int):
    if disk_type == 0x00:
        return "sony400k"
    elif disk_type == 0x01:
        return "sony800k"
    elif disk_type == 0x02:
        return "720k"
    elif disk_type == 0x03:
        return "profile-i-guess"
    elif disk_type == 0x5D:
        return "profile-i-guess"
    elif disk_type == 0x54:
        return "twiggy872k"
    else:
        return "UNKNOWN!"


# The "filetype" enum (see LISA_OS/OS/source-sfileio.text.unix.txt):
FILETYPE_NAMES = {
    1: "MDDFfile",
    2: "rootcat",
    3: "freelist",
    4: "badblocks",
    5: "sysdata",
    6: "spool",
    7: "exec",
    8: "userdir",
    9: "pipe",
    10: "bootfile",
    11: "swapdata",
    12: "swapcode",
    13: "ramap",
    14: "userfile",
    15: "killedobject",
    16: "tempfile",
}


def file_system_version_to_string(fs_version: int):
    if (
        fs_version == 17
    ):  # aka SPRING_VERSION in file LISA_OS/OS/SOURCE-VMSTUFF.TEXT.unix.txt
        return "LOS 3.1"
    elif fs_version == 16:
        return "LOS 3.0"
    elif fs_version == 15:  # aka PEPSI_VERSION
        return "LOS 2.0"
    elif fs_version == 14:  # aka REL1_VERSION
        return "LOS 1.0"
    else:
        return "UNKNOWN!"


def tag_size_to_string(tag_size: int):
    if tag_size == 12:
        return "Floppy disk image"
    elif tag_size == 20:
        return "ProFile hard disk image"
    else:
        return "UNKNOWN!"


def disk_format_to_string(disk_format: int):
    """
    There are a few other known formats
    that are not below, see
    https://www.discferret.com/wiki/Apple_DiskCopy_4.2
    """
    if disk_format == 0x02:
        return "sony400k"
    elif disk_format == 0x22:
        return "sony800k"
    elif disk_format == 0x01:
        return "twiggy872k"
    elif disk_format == 0x93:
        return "profile-i-guess"
    elif disk_format == 0x96:
        return "profile-i-guess"
    else:
        return "UNKNOWN!"


def file_id_type_to_string(file_id_: int):
    if file_id_ == 0x0000:
        return "FILEID_FREE"
    elif file_id_ == 0xAAAA:
        return "FILEID_BOOT"
    elif file_id_ == 0xBBBB:
        return "FILEID_LOADER"
    elif file_id_ == 0x0001:
        return "FILEID_MDDF"
    elif file_id_ == 0x0002:
        return "FILEID_BITMAP"
    elif file_id_ == 0x0003:
        return "FILEID_SRECORD"
    elif file_id_ == 0x0004:
        return "FILEID_CATALOG"
    elif file_id_ == -21846:
        return "FILEID_BOOT_SIGNED"
    elif file_id_ == -17477:
        return "FILEID_LOADER_SIGNED"
    elif file_id_ == 0x7FFF:
        return "FILEID_ERASED"
    else:
        # some file id different from the above, which indicates that this is a regular file.
        return f"{file_id_:#06x}"


def derive_mddf_sector_number_from_boot_sector_zero(
    boot_sector_bytes: bytes, num_sectors: int
) -> int:
    """Derive the MDDF (Media Descriptor Data File) sector number from the boot sector (sector 0),
    exactly like the Lisa boot ROM and boot loader do. See LISA_OS/OS/source-LDEQU.TEXT.unix.txt:
    the "Write Boot Tracks" utility (PWBT in SOURCE-FSINIT1.TEXT.unix.txt) stores the "block
    address of MDDF" (fs_block0) in the boot sector, and the LOADER reads the MDDF from
    absolute sector (vol_starts + fs_block0). Three boot sector layouts exist:
      * current (LOS 2.0+):              boot_id $AAAA at byte offset 4, fs_block0 at byte offset 14;
      * older (LOS 1.0/1.2, profile):    no $AAAA magic, starts with "jmp $0012", fs_block0 at byte offset 10;
      * older (LOS 1.0/1.2, Twiggy/Sony): no $AAAA magic, starts with "jmp $000A", fs_block0 at byte offset 10.

    vol_starts is 8 for Profile/Widget and hard disk images (the logical volume starts at
    sector 8, skipping the mount table at sector 7; see source-LDPROF.TEXT.unix.txt), and
    0 for floppy images (see source-LDTWIG.TEXT.unix.txt and source-ldmicro.text.unix.txt).
    The disk type is determined by the number of sectors: 800/1600/1702 = floppy, anything
    else (e.g. 9728/19456 = profile, 94208 = 50MB hard disk) = profile or hard disk.

    Parameters:
      boot_sector_bytes: the 512 bytes of sector 0.
      num_sectors: the total number of sectors in the image.

    Returns the MDDF sector number or -1 if it cannot be found.
    """
    boot_id = to_uint16_big_endian(boot_sector_bytes, 4)
    fs_block0 = None
    if boot_id == 0xAAAA:
        print(
            "\nBoot sector 0 uses the latest (LOS 2.0+) boot sector layout; fs_block0 is read from byte offset 14."
        )
        fs_block0 = to_uint16_big_endian(boot_sector_bytes, 14)
    elif boot_sector_bytes[0:2] == b"\x4e\xfa":
        # Older layouts (LOS 1.0/1.2) also start with a "jmp" instruction
        # ("jmp $0012" on profile disks, "jmp $000A" on Twiggy/Sony floppies);
        # both store fs_block0 at byte offset 10.
        print(
            "\nBoot sector 0 uses the older (LOS 1.0/1.2) boot sector layout; fs_block0 is read from byte offset 10."
        )
        fs_block0 = to_uint16_big_endian(boot_sector_bytes, 10)
    if fs_block0 is not None:
        if num_sectors in (800, 1600, 1702):
            print(
                f"Found fs_block0: {fs_block0}; This is a floppy disk image, so the MDDF sector number is {fs_block0} (is at fs_block0)"
            )
            mddf_sector_number = fs_block0  # floppy image
        else:
            print(
                f"Found fs_block0: {fs_block0}; This is a profile hard disk image, so the MDDF sector number is {fs_block0 + 8} (is at fs_block0 + 8)"
            )
            mddf_sector_number = fs_block0 + 8  # profile or hard disk image
    else:
        mddf_sector_number = -1
    return mddf_sector_number


def to_uint32_big_endian(byte_array, offset):
    """Reads 4 bytes at the given offset and converts them to unsigned int."""
    return struct.unpack(">I", byte_array[offset : offset + 4])[0]


def three_bytes_to_int_big_endian(byte_array, offset):
    return int.from_bytes(
        byte_array[offset : offset + 3], byteorder="big", signed=False
    )


def to_uint16_big_endian(byte_array, offset):
    """Reads 2 bytes at the given offset and converts them to unsigned int."""
    return struct.unpack(">H", byte_array[offset : offset + 2])[0]


def to_uint32_little_endian(byte_array, offset):
    """Reads 4 bytes at the given offset and converts them to unsigned int."""
    return struct.unpack("<I", byte_array[offset : offset + 4])[0]


def read_and_print_bytes_in_hex_and_ascii(
    file: BinaryIO, offset: int, bytes_to_print: int
):
    """
    Reads exactly bytes_to_print bytes from a BinaryIO object and prints them
    16 bytes per line, showing both hexadecimal and ASCII representations.

    Args:
        file: A BinaryIO object (e.g., an opened file in binary mode,
              or an io.BytesIO object),
        offset: where to start printing at,
        bytes_to_print: how many bytes to print
    """
    chunk_size = 16
    file.seek(offset)
    # Read the specified number of bytes from the file object
    data = file.read(bytes_to_print)

    # Check if we actually read 512 bytes. If not, print a warning.
    if len(data) < bytes_to_print:
        print(f"Warning: Only {len(data)} bytes read, expected {bytes_to_print}.")
        # Adjust bytes_to_read to the actual number of bytes read for the loop
        bytes_to_print = len(data)
        if bytes_to_print == 0:
            print("No data to display.")
            return

    print(f"--- Displaying {bytes_to_print} bytes ---")

    # Iterate through the data in chunks of 16 bytes
    for i in range(0, bytes_to_print, chunk_size):
        # Get the current 16-byte chunk
        chunk = data[i : i + chunk_size]

        # --- Hexadecimal representation ---
        # Convert each byte in the chunk to a two-digit hexadecimal string,
        # then join them with spaces.
        hex_representation = " ".join(f"{byte:02x}" for byte in chunk)

        # --- ASCII representation ---
        ascii_representation = ""
        for byte_val in chunk:
            # Check if the byte represents a printable ASCII character
            if 32 <= byte_val <= 126:  # ASCII printable range
                ascii_representation += chr(byte_val)
            else:
                ascii_representation += (
                    "."  # Replace non-printable characters with a dot
                )

        # Print the current line: offset, hexadecimal, and ASCII
        # The offset is formatted to be 4 hexadecimal digits (e.g., 0000, 0010, etc.)
        print(f"{i:04x}: {hex_representation:<48} | {ascii_representation}")

    print(f"--- End of data ---")


def print_bytes_in_hex_and_ascii(bytes_to_print):
    chunk_size = 16
    print(f"--- Displaying {len(bytes_to_print)} bytes ---")
    for i in range(0, len(bytes_to_print), chunk_size):
        # Get the current 16-byte chunk
        chunk = bytes_to_print[i : i + chunk_size]
        hex_representation = " ".join(f"{byte:02x}" for byte in chunk)
        ascii_representation = ""
        for byte_val in chunk:
            # Check if the byte represents a printable ASCII character
            if 32 <= byte_val <= 126:  # ASCII printable range
                ascii_representation += chr(byte_val)
            else:
                ascii_representation += (
                    "."  # Replace non-printable characters with a dot
                )
        print(f"{i:04x}: {hex_representation:<48} | {ascii_representation}")
    print(f"--- End of data ---")


def print_fields_in_byte_array(field_definitions, byte_array, start_offset: int):
    """
    Parses a byte array based on a list of field definitions and prints
    the formatted hexadecimal value for each field.

    Args:
        field_definitions (list): A list of lists/tuples, where each inner
                                  item contains the field name (str),
                                  start offset (int), and length (int).
                                  Example: [['field1', 0, 4], ['field2', 4, 2]]
        byte_array (bytes): The byte array or bytes-like object to parse.
    """
    for index, array_entry in enumerate(field_definitions):
        name = array_entry[0]
        offset = array_entry[1]
        # We derive the length of the current field as: next field's offset minus the current offset:
        length = (
            (field_definitions[index + 1][1] - offset)
            if index < (len(field_definitions) - 1)
            else 2
        )
        if start_offset + offset + length > len(byte_array):
            print(f"{name}: ERROR - Field is out of bounds.")
            continue
        offset_as_hex = f"0x{offset:04X}"
        data_slice = byte_array[start_offset + offset : start_offset + offset + length]

        # --- Format and print the output ---
        # Convert the byte slice to an uppercase hexadecimal string.
        hex_value = data_slice.hex().upper()
        if name.startswith("DT_") and length <= 4:
            decimal_value = int.from_bytes(data_slice, "big")
            print(
                f"At {offset:>4} ({offset_as_hex}) : {name:>20}: 0x{hex_value} (date '{format_date(decimal_value)}')"
            )
        elif name == "init_machine_id" or name == "master_machine_id":
            machine_id = int.from_bytes(
                data_slice, "big"
            )  # (the machine this file may be opened on, if the file is protected)
            # Per LISA_OS/OS/source-SERNUM.TEXT.unix.txt: machine_id = first3 * 65536 + last5, where
            # first3/last5 are the BCD digits of the 8-digit AppleNet serial number.
            applenet_id = (
                f"AppleNet '{machine_id // 65536:03d}{machine_id % 65536:05d}'"
            )
            print(
                f"At {offset:>4} ({offset_as_hex}) : {name:>20}: 0x{hex_value} ({applenet_id})"
            )
        elif length <= 4:
            # Convert bytes to an integer. 'big' means the most significant byte is at the beginning of the byte array (aka big-endian).
            decimal_value = int.from_bytes(data_slice, "big")
            print(
                f"At {offset:>4} ({offset_as_hex}) : {name:>20}: 0x{hex_value} (Decimal: {decimal_value})"
            )
        elif name in ("volname", "password"):
            print(
                f"At {offset:>4} ({offset_as_hex}) : {name:>20}: 0x{hex_value} (string '{pascal_to_string(data_slice)}')"
            )
        else:
            print(f"At {offset:>4} ({offset_as_hex}) : {name:>20}: 0x{hex_value}")


def format_date(date_as_int: int) -> str:
    """Convert a Lisa timestamp to a local-time string.

    A Lisa timestamp is the number of seconds since the midnight prior to
    1 January 1901 (NOT 1900!), in GMT. See LISA_OS/LIBS/LIBHW/libhw-TIMERS.TEXT.unix.txt
    ("number of seconds since the midnight prior to 1 January 1901") and
    LISA_OS/OS/source-TIMEMGR.TEXT.unix.txt (baseyear = 1901).
    Lisa stored times in GMT (see GET_TIME in LISA_OS/OS/source-clock.text.unix.txt)
    and converted GMT -> local time for display (Convert_Time in timemgr,
    LISA_OS/GUIDE_APIM/apim-tsettime.TEXT.unix.txt), which we approximate here
    with the system's local timezone.
    """
    if date_as_int == 0:
        return "undefined"
    unix_timestamp = (
        date_as_int - 2177452800
    )  # this is the 1901->1970 offset (same ADJUST as in lisafsh-tool.c)
    # Convert the Unix timestamp to a timezone-aware datetime object
    #    in UTC, then convert it to the system's local timezone.
    #    This step mimics C's 'localtime(&t)' which gets the local time
    #    structure, respecting Daylight Saving Time.
    dt_object_utc = datetime.fromtimestamp(unix_timestamp, tz=timezone.utc)
    dt_object_local = (
        dt_object_utc.astimezone()
    )  # Converts to the system's local timezone
    # Format the datetime object into the desired string format.
    #    This is equivalent to C's strftime().
    #    The format codes are largely the same.
    formatted_date_string = dt_object_local.strftime("%Y.%m.%d-%H:%M")
    return formatted_date_string


def compare_filenames(a: str, b: str) -> bool:
    """
    Compares two strings 'a' and 'b' case-insensitively to determine
    if 'a' comes lexicographically before 'b'.

    This function mimics the behavior of the provided C function:
    - It performs a character-by-character comparison after converting
      characters to uppercase.
    - If characters are equal up to the length of the shorter string,
      the shorter string is considered to come first.

    Args:
        a: The first string.
        b: The second string.

    Returns:
        True if string 'a' comes before string 'b' lexicographically
        (case-insensitively), False otherwise.
    """
    # Get the lengths of the strings
    a_len = len(a)
    b_len = len(b)

    # Determine the minimum length to iterate up to
    min_len = min(a_len, b_len)

    # Iterate through the strings up to the minimum length
    for i in range(min_len):
        # Convert characters to uppercase for case-insensitive comparison
        # In Python, you can directly call .upper() on a single character string
        ac = a[i].upper()
        bc = b[i].upper()

        # If characters at the current position are different,
        # their comparison determines the order
        if ac != bc:
            return ac < bc

    # If the loop completes, it means the strings are identical
    # up to the length of the shorter string.
    # In this case, the shorter string comes lexicographically before the longer one.
    return a_len < b_len


_INTERLIEVED_SECTORS_OFFSET_DELTA = (
    0,
    4,
    8,
    12,
    0,
    4,
    8,
    -4,
    0,
    4,
    -8,
    -4,
    0,
    -12,
    -8,
    -4,
)


def interleave5(sector: int) -> int:
    """
    What is "5:1" interleaving?
    The original ProFile hard disk drive used a 5:1 interleaving scheme to optimize data retrieval times:
    By the time the Lisa processed the sector it just read, the Profile disk platter has moved few sectors further along.
    To avoid waiting for a nearly-full disk rotation to read the next sector, the sectors are interleaved.
    This optimization speeds up sequential reads.

    For sector number  0: interleave(0)=0
    For sector number  1: interleave(1)=5
    For sector number  2: interleave(2)=10
    For sector number  3: interleave(3)=15
    For sector number  4: interleave(4)=4
    For sector number  5: interleave(5)=9
    For sector number  6: interleave(6)=14
    For sector number  7: interleave(7)=3
    For sector number  8: interleave(8)=8
    For sector number  9: interleave(9)=13
    For sector number 10: interleave(10)=2
    For sector number 11: interleave(11)=7
    For sector number 12: interleave(12)=12
    For sector number 13: interleave(13)=1
    For sector number 14: interleave(14)=6
    For sector number 15: interleave(15)=11
    For sector number 16: interleave(16)=16
    For sector number 17: interleave(17)=21
    For sector number 18: interleave(18)=26
    For sector number 19: interleave(19)=31
    For sector number 20: interleave(20)=20
    ... etc ...
    """
    return (
        sector + _INTERLIEVED_SECTORS_OFFSET_DELTA[sector & 15]
    )  # "sector & 15" is an optimized version of "sector % 16"


# Example usage:
if __name__ == "__main__":
    available_commands = (
        "deserialize",
        "info",
        "list",
        "dump",
        "dump-flatten",
        "fix_dc42_checksum",
    )

    if len(sys.argv) != 3:
        print("Usage: python LisaFileSystemTool.py <command> <disk image file name>")
        print("Available commands:")
        print(
            "  deserialize    Remove the theft-protection from all protected files found on the disk image."
        )
        print(
            "  info           Print disk image info (file format, checksums, MDDF sector)."
        )
        print("  list           List the files on the disk image.")
        print(
            "  dump           Dump all files from the disk image into folder /tmp/LisaFileSystemDump"
            " (a '/' in a Lisa file name becomes a subfolder there)."
        )
        print(
            "  dump-flatten   Like 'dump', but a '/' in a Lisa file name is replaced with a '-'"
            ", so all files are dumped into the /tmp/LisaFileSystemDump folder (no subfolders are being created)."
        )
        print(
            "                 Note: two file names that differ only by '/' vs '-' (e.g. 'a/b.txt'"
            " and 'a-b.txt') map to the same host file; the one dumped later overwrites the earlier one."
        )
        print(
            "  fix_dc42_checksum  Fix the DC42 header data/tags checksums, but only if they are wrong."
        )
        sys.exit(1)

    command = sys.argv[1].strip().lower()
    file_name = sys.argv[2]

    if command not in available_commands:
        print(
            f"ERROR: unknown command '{command}'. Available commands: {', '.join(available_commands)}"
        )
        sys.exit(1)

    # The top level is the right place to turn a failed construction (FileNotFoundError,
    # ValueError, ...) into a one-line error message and a non-zero exit status.
    # FileSystem.__init__ itself just raises, so the class stays usable from other code.
    try:
        file_system = InMemoryFileSystem(file_name)
    except FileNotFoundError:
        print(f"File {file_name} not found!")
        sys.exit(1)
    except IOError as e:
        print(f"IO Error: '{e}' while reading file {file_name} !")
        sys.exit(1)
    except Exception as e:
        print(
            f"ERROR: {type(e).__name__}: {e} while reading file {file_name}! Exiting."
        )
        sys.exit(1)

    if command == "deserialize":
        try:
            if file_system._confirm_proceed_if_disk_image_is_open_by_other_process():
                file_system.remove_file_protection()
            else:
                print("Aborted by user; the disk image was not modified.")
        except EOFError:
            # No interactive terminal (stdin closed/redirected): don't prompt, just skip.
            print(
                "No interactive terminal for the confirmation prompt; skipping remove_file_protection()."
            )
    elif command == "info":
        file_system.print_extra_info()
    elif command == "list":
        if file_system.is_flat_catalog_volume():
            file_system.flat_catalog_list_files()
        else:
            file_system.dump_catalog()
    elif command == "dump":
        file_system.dump_files()
    elif command == "dump-flatten":
        file_system.dump_files(flatten=True)
    elif command == "fix_dc42_checksum":
        try:
            file_system.fix_dc42_checksum()
        except EOFError:
            # No interactive terminal (stdin closed/redirected): don't prompt, just skip the fix.
            print(
                "No interactive terminal for the confirmation prompt; skipping the DC42 checksum fix."
            )
    # That's all, Folks!
