# LisaFileSystemTool — a tool for reading and modifying the Lisa OS File System disk images

**Author:** [TorZidan](https://github.com/TorZidan)  
**Last Updated:** Sept 23, 2026  

## Overview

Modern floppy and Lisa hard disk hardware emulators (ESProfile, ESFloppy), FloppyEmu) are extremely popular in the Lisa enthusiasts community.
They all store the disk volume on an SD card, in a specific format (either "DC42" or "raw ProFile").
The LisaEm emulator uses these very same disk image formats.
Given that these disk image files are of size just a few megabytes, uses are able to freely share them on the webs.
It's been a natural curiosity for most to try to understand the format of these image files.

There has been limited success until now, mostly due to the lack of documentation of these HFS-style (Hierarchical File System) B-tree catalog volumes.
The advent of AI and Apple's release of the Lisa OS source files (In January of 2023) have given a boost to these efforts.

The presented `LisaFileSystemTool.py` is a standalone Python tool for inspecting and modifying
Apple Lisa Office System (aka LOS) and Workshop disk images. It understands
two image container formats (DC42 and Raw ProFile), works with both floppy and ProFile/hard-disk
volumes, and works with every known LOS file-system version (v14 = LOS 1.0 through v17 = LOS 3.1,
i.e. both the old "flat catalog" volumes and the newer HFS-style B-tree catalog volumes). 

A companion tool, `LisaFileSystemToolPerFile.py` (described in §1.3), extends it with the
`add` file, `replace` file and `delete` file commands: it can add a new file to a disk image,
replace the contents of an existing file, or delete a file, on both flat-catalog (LOS 1.0/2.0)
and B-tree (LOS 3.0/3.1) hard-disk volumes.
It is a thin extension of `LisaFileSystemTool.py` — all the disk-image machinery is imported
from there, and only the add/replace/delete-specific code lives in the second file.

The tool can remove file copy protection from such disk images (if any is present), which makes
them more universal: they are no-longer tied to a specific Lisa serial number, and thus can be used (run successfully) on any real and emulated Lisa. 
Disclaimer: given that the Lisa OS source files are open-source and free-to-use now               
(at https://info.computerhistory.org/apple-lisa-code), we believe that removing file copy         
protection causes no harm or loss to Apple.

The document has two parts:

1. **Using the tool** (commands, behavior, output).
2. **The Lisa OS file system itself** — boot sector, MDDF, tags, file IDs, s-list records,
   catalog, hint sectors, protected files, etc. — as implemented by the Lisa OS and as
   decoded by this tool.

---

## 1. Using the tool

### 1.1 LisaFileSystemTool.py - listing and dumping all files, deserializing LOS applications, and more

```
python3 LisaFileSystemTool.py <command> <disk image file name>
```

| Command     | What it does |
|-------------|--------------|
| `info`      | Prints useful information about the disk image (format, sector count, disk name, DC42 checksums, disk type/format), then prints the MDDF fields (volume name, version, slist/bitmap/catalog pointers, …) and lists the sector numbers of each predefined sector type (MDDF, bitmap, s-record, catalog, boot, loader, erased, free). |
| `list`      | Lists all files on the volume. For file systems of version 16/17 it walks the B-tree catalog (`dump_catalog()`); for file systems of version 14/15 it scans the s-list of the flat-catalog volume (`flat_catalog_list_files()`). |
| `visualize` | Prints a one-character-per-sector map of the whole volume, 64 sectors per line. |
| `dump`      | Writes the contents of every file to the host folder **`/tmp/LisaFileSystemDump/`** (created if missing), preserving the names as stored on disk. The root catalog file itself is skipped, and "empty" file names are skipped. Text files (with file names ending with ".text" ) are dumped as plain host text: the 1024-byte on-disk header page and the null page padding are stripped, and the \r new-line symbols used by LOS are converted to \n, so no further conversion is needed. |
| `dump-flatten` | Like `dump`, but a '/' in a Lisa file name is replaced with '-', so all files are dumped directly into **`/tmp/LisaFileSystemDump/`** (no subfolders). |
| `deserialize` | Finds all theft-protected files (see §5.9), asks for y/N confirmation, then rewrites each file's hint sector (so the file can be opened on any machine), and fixes up all affected checksums. |
| `fix_dc42_checksum`| For DC42 files: checks if the data and tag checksums are correct in the DC42 header, and fixes the incorrect ones, if any. |


### 1.2 LisaFileSystemToolPerFile.py  — adding, replacing and deleting files

`LisaFileSystemToolPerFile.py` is a companion tool that **writes** files onto a disk image.
It is a thin extension of `LisaFileSystemTool.py` (all the disk-image machinery code is imported
from there; only the add/replace/delete-specific code lives in this file).

```
python3 LisaFileSystemToolPerFile.py add     <disk image file name> <host file name> <lisa file name>
python3 LisaFileSystemToolPerFile.py replace <disk image file name> <host file name> <lisa file name>
python3 LisaFileSystemToolPerFile.py delete  <disk image file name> <lisa file name>
```

| Command   | What it does |
|-----------|--------------|
| `add`     | Adds the host file to the volume as a new file with the given Lisa name. A new s-file number, hint and data pages, and a catalog entry are allocated and written, and all affected checksums are fixed up. If a file with the same name already exists, nothing is written. |
| `replace` | Replaces the contents of an existing file (found by name, case-insensitive) in place, reusing the file's existing sectors: if the new file is larger, only the extra sectors are newly allocated; if it is smaller, the unused old sectors are freed. If no file with that name exists, nothing is changed. |
| `delete`  | Deletes a regular file (found by name, case-insensitive): the catalog entry is removed, the file's data and hint pages are released to the free pool, and the s-list sentry is emptied, exactly as the OS `kill_sfile`/`Ddelete` paths do. On B-tree volumes the catalog record is removed with the OS b-tree deletion algorithm (including the underflow rebalancing: merge or rotate with the sibling, propagation up to the root, and tree-depth shrink when the root becomes empty); freed catalog nodes are zeroed and their pages returned to the free pool. On flat-catalog volumes the centry is cleared per the OS `KILL_ENTRY` rules (including entries that straddle catalog page boundaries). Directories and other non-file catalog entries are rejected. The MDDF counters (`filecount`, `freecount`, `fs_overhead`, `empty_file`, and — on B-tree volumes — `root_page`/`tree_depth`) are all kept consistent, and every affected sector's tag checksum and the DC42 checksums (if any) are fixed up. If the MDDF `tree_depth` field turns out to be stale (e.g. after an interrupted operation), it is corrected from the actual tree structure and a warning is printed. |

Exit codes: `0` = success; `3` = nothing was done (`add`: a file with that name already exists /
`replace`: no file with that name on the volume / `delete`: no file with that name on the volume);
`1` = any other failure (including: `delete` given the name of a directory or other non-file
catalog entry). 

### 1.3 Lisa Text File format

If the Lisa file name ends with ".TEXT" (case-insensitive), the code assumes that it's a text file.
Text files are stored in a special way on the Lisa file system (see [LisaOsTextFileSpecification.txt](./LisaOsTextFileSpecification.txt)),
so, unlike binary files, there is some extra work we need to do when adding/replacing text files: the host text file is
converted to the on-disk Lisa text layout : line endings become \r, the text is laid out in 1024-byte "pages" of \r-terminated lines with an
all-zero 1024-byte header "page" prepended, and the stray trailing 0xFF byte that the Computer
History Museum Lisa source archive text files have is stripped (if any). Together with the `dump`
command of `LisaFileSystemTool.py` (which converts a .TEXT file back to host text), this makes
`dump` → edit on the host → `add`/`replace` a good round trip.

### 1.4 Accepted input files

* **DC42 images** (`.dc42`) — Apple DiskCopy 4.2 format; the normal way LisaEm
  distributes floppy and hard-disk images.
* **Raw ProFile hard-disk images**  (usually `.image`) — a sequence of `20-byte tag + 512-byte sector data`
  records, physically stored in 5:1 interleave order (see §4.2 and §4.4).

The disk image type is auto-detected: if the 2-byte `fileFormat` field at offset `0x52` is
`0x0100` (aka DC42 magic number), the file is treated as DC42, otherwise as a raw ProFile image.

Practical limits: the whole disk image file is read into memory; files longer than
100 000 000 bytes are rejected; DC42 images with `tagSize == 0` are rejected (the tool
needs the tag fields to navigate the file system).

### 1.5 Companion tool: LisaSerialNumberTool.py - prints/decodes Lisa serial numbers

`LisaSerialNumberTool.py` is a standalone Python tool for decoding the Apple Lisa VSROM
(Video State ROM) serial number, given as the 32-character hex string (16 bytes, "32 nibbles")
used by the LisaEm emulator (File -> Preferences). It prints a human-readable report: the two
16-byte serial lines as shown by the Lisa in service mode (addresses 0x240 and 0x250), the
decoded plant / year / day / serial-number fields, the 8-digit BCD AppleNet number (plus the
`machine_id` derived from it), and the result of the checksum replicated from the boot ROM's
SERNUM routine. If the checksum is invalid, it also prints a "corrected" serial number whose
checksum passes (the 3 BCD checksum nibbles are rewritten with the computed checksum).

```
python3 LisaSerialNumberTool.py <serial number>
# e.g. python3 LisaSerialNumberTool.py FF028308104050FF0010163504700000
```

---

## 2. The image container formats

### 2.1 DC42 (DiskCopy 4.2)

The first `0x54` (84) bytes are a fixed header:

| Offset | Size | Field | Meaning |
|--------|------|-------|---------|
| `$00`  | `$40` | `diskName`   | Pascal string (1 length byte + up to 63 ASCII chars). For Lisa disks typically `"-not a Macintosh disk-"`. |
| `$40`  | 4     | `dataSize`   | Total bytes of the **data** fields. A data field is the 512-byte sector data, so `dataSize = 512 × number of sectors`. |
| `$44`  | 4     | `tagSize`    | Total bytes of the **tag** fields: 12 bytes per sector (floppy) or 20 bytes per sector (ProFile/hard disk). Zero if the image has no tags. |
| `$48`  | 4     | `dataChecksum` | Checksum of all data fields (see below). |
| `$4C`  | 4     | `tagChecksum`  | Checksum of all tag fields (see below). |
| `$50`  | 1     | `diskType`   | `$00` sony 400k, `$01` sony 800k, `$02` 720k, `$03` profile, `$54` twiggy 872k. |
| `$51`  | 1     | `formatByte` | `$02` sony 400k, `$22` sony 800k, `$01` twiggy 872k, `$96` profile. |
| `$52`  | 2     | `fileFormat` | Always `$0100` (the DC42 magic number the tool looks for). |

Immediately after the header come, in order:

* **the data fields** — 512 bytes per sector, sector 0 through the last one, contiguous;
* **the tag fields** — one 12- or 20-byte tag per sector, in the same order, *all tags
  after all data*.

### 2.2 Raw ProFile hard-disk images

A raw image has no header: it is just a sequence of sectors, each
`20-byte tag` **followed by** its `512-byte sector data` (tag first, data second).
However, the records are not in logical sector order: the original ProFile drive used a
**5:1 interleave** so the next sector is physically a few tracks away, avoiding waiting
for a full hard disk platter turn. Logical sector `n` is stored at physical position
`interleave5(n)`:

```
interleave(0)=0, 1=5, 2=10, 3=15, 4=4, 5=9, 6=14, 7=3, 8=8, 9=13,
10=2, 11=7, 12=12, 13=1, 14=6, 15=11, 16=16, 17=21, ...
```

(in general `interleave5(n) = n + delta[n mod 16]` with
`delta = (0, 4, 8, 12, 0, 4, 8, -4, 0, 4, -8, -4, 0, -12, -8, -4)`).

Therefore in a raw image:

```
tag of sector n  : offset  interleave5(n) * 532
sector n data    : offset  interleave5(n) * 532 + 20
```

---

## 3. The Boot sector and locating the MDDF (Media Descriptor Data File)

Sector 0 is the boot sector. Two layouts exist (see
`LISA_OS/OS/source-LDEQU.TEXT.unix.txt`; the "Write Boot Tracks" utility, PWBT in
`SOURCE-FSINIT1.TEXT.unix.txt`, stores the "block address of MDDF" — `fs_block0` —
there, and the loader reads the MDDF from `vol_starts + fs_block0`):

| Layout | Recognition | `fs_block0` at offset |
|--------|-------------|----------------------|
| current (LOS 2.0+) | big-endian `0xAAAA` at byte offset 4 | 14 |
| older (LOS 1.0/1.2, ProFile) | starts with `jmp $0012` (`4E FA 00 12`) | 10 |
| older (LOS 1.0/1.2, Twiggy/Sony) | starts with `jmp $000A` (`4E FA 00 0A`) | 10 |

`vol_starts` is **8** for ProFile/Widget and hard-disk images (the logical volume
starts at sector 8, skipping the mount table at sector 7 —
`source-LDPROF.TEXT.unix.txt`) and **0** for floppies (`source-LDTWIG.TEXT.unix.txt`,
`source-ldmicro.text.unix.txt`). The tool decides "floppy" from the sector count
(800/1600/1702) and "hard disk" otherwise.

So: `MDDF sector = fs_block0 + (8 or 0)`. As a fallback (e.g. damaged boot sector) the
tool scans the tags of **all** sectors for `file_id 0x0001` and takes the first hit.

---

## 4. The MDDF (Media Descriptor Data File) sector

The MDDF (Media Descriptor Data File) data structure is created when the volume is first initialized; it
describes everything about the media: size, page layout, and where the file-system
structures live. 
It occupies one sector, but may be "backed up" onto other sectors; the tool reads the first one.
Its tag has a file_id of `FILEID_MDDF` = `0x0001`.
The field offsets used by the tool (in `MDDF_FIELD_DEFINITIONS`) were
discovered from real Lisa profile disk images, because the offsets in the published
Pascal record (`SOURCE-VMSTUFF.TEXT.unix.txt`) do not quite match real images. Fields
marked "verified" in the source comments are the ones the tool actually relies on / modifies.

Key fields (byte offsets within the MDDF sector):

| Offset | Size | Field | Meaning |
|--------|------|-------|---------|
| 0x00 | 2 | `fsversion` | file-system version: **14 = LOS 1.0** (`REL1_VERSION`), **15 = LOS 2.0** (`PEPSI_VERSION`), **16 = LOS 3.0**, **17 = LOS 3.1** (`SPRING_VERSION`). The tool accepts 14..17. |
| 0x02 | 8 | `volid` | volume UID (two 32-bit longs) |
| 0x0A | 2 | `volnum` | volume number |
| 0x0C | 34 | `volname` | Pascal string, up to 33 chars |
| 0x2E | 34 | `password` | volume password (Pascal string) |
| 0x50 | 4 | `init_machine_id` | machine the volume was initialized on |
| 0x54 | 4 | `master_machine_id` | "for theft protection" |
| 0x58…0x67 | 4 each | `DT_created`, `DT_copy_created`, `DT_copied`, `DT_scavenged` | dates (see §6.3) |
| 0x6C | 4 | `firstblock` / `lastblock` (0x6C/0x6E) | first/last absolute block of the volume |
| 0x74 | 4 | `blockcount` | number of blocks on the disk |
| 0x78 | 4 | `blocksize` | bytes per block (512) |
| 0x80 | 4 | `MDDFaddr` | page where the MDDF starts |
| 0x84 | 4 | `MDDFsize` | size in bytes of the MDDF |
| 0x88 | 4 | `bitmap_addr` | MDDF-relative page where the allocation bitmap starts (usually **1**, i.e. immediately after the MDDF) |
| 0x8C | 4 | `bitmap_size` | size **in bits** of the allocation bitmap (≈ one bit per sector) |
| 0x90 | 2 | `bitmap_bytes` | bitmap size in bytes |
| 0x92 | 2 | `bitmap_pages` | number of bitmap sectors |
| 0x94 | 4 | `slist_addr` | MDDF-relative page where the s-file table starts |
| 0x98 | 2 | `slist_packing` | s_entry records per slist page (usually **36**; 36 × 14 = 504 ≤ 512) |
| 0x9A | 2 | `slist_block_count` | number of slist pages |
| 0x9C | 2 | `first_file` | fid of the minimum non-initial s-file (on flat-catalog volumes: the root catalog itself) |
| 0x9E | 2 | `empty_file` | minimum fid of all unused s-file ids |
| 0xA0 | 2 | `maxfiles` | maximum s-file index on this device |
| 0xA2 | 2 | `hintsize` | number of pages in s-file hints |
| 0xA4/0xA6 | 2 | `leader_offset`, `leader_pages` | file leader location/size in hint pages |
| 0xA8 | 2 | `flabel_offset` | byte offset of the file label in the first hint page |
| 0xAC/0xAE | 2 | `map_offset`, `map_size` | first file map page offset / entries per page |
| 0xB0 | 2 | `filecount` | current number of s-files allocated |
| 0xB2 | 4 | `freestart` | start of free-page search in the bitmap |
| 0xBA | 4 | `freecount` | number of free pages |
| 0xBE | 2 | `rootsnum` | s-file number of the root catalog file |
| 0xC0 | 2 | `rootmaxentries` | max number of centries in the root catalog |
| 0xC2 | … | `mountinfo`, `overmount_stamp`, `pmem_id`, `pmem` (65 bytes) | mount state / parameter memory |
| 0x112 | 1 | `vol_scavenged` / `tbt_copied` | booleans |
| 0x114 | 2 | `smallmap_offset` | byte offset of the small file map in the hint page |
| 0x116 | 2 | `hentry_offset` | byte offset of the hentry in the hint page |
| 0x118 | 8 | `backup_volid` | volume this volume backs up |
| 0x122 | 2 | `flabel_size` | byte size of the file label |
| 0x124 | 2 | `fs_overhead` | per-volume file-system space overhead |
| 0x126 | 2 | `result_scavenge` | result of the last scavenge |
| 0x128 | 2 | `boot_code` / `boot_environ` | reserved |
| 0x12A | 2 | `oem_id` | OEM id |
| 0x12E | 4 | `root_page` | **MDDF-relative page number of the B-tree catalog root** (fs 16/17) |
| 0x132 | 2 | `tree_depth` | catalog B-tree depth |
| 0x134 | 4 | `node_id` | node id |
| 0x136 | 4 | `vol_seq_no` | volume sequence number |
| 0x138 | 4 | `vol_mounted` | mounted flag |

---

## 5. The file-system structures

### 5.0 Sectors and Tags

A disk image is a set of disk sectors of size 512 bytes each.
Every disk sector is accompanied by it's "tag data" (of size 12 bytes on floppy disk images or 20 bytes on hard disk images).
The sectors are referred as "pages", and the tags are referred as "page descriptors" in the LOS sources.

The tag data for a given sector, most importantly, contains the "previous" and "next" sector numbers.
For example, to read a file, we get it's first data sector number from the file's "s_entry" record, then get the tag data for this sector, find the next data sector number in there, and loop until we see next data sector of of 00FFFFFF (end of file). 

The Lisa OS file system also uses the tag data for data recovery (aka "scavenging"), when needed: If LOS was not shut down gracefully (by pressing the Lisa Power button and patiently waiting for it to "put everything away"), upon the next restart it will alert you. Since this is the active boot volume, LOS will not be able to repair it, and will prompt you to insert and boot the LOS Installation Floppy Disk 1, which has a "Repair" button, which traverses the tag data for each sector, to repair the disk volume (as much as it can); Files that were repaired have their "scavenged" attribute set (it can be seen in Workshop -> File Manager -> List files).

**Absolute vs relative sector numbers.** This is a recurring source of confusion. A
*relative* (or "MDDF-relative") sector/page number is counted from the MDDF sector, not
from the start of the image. Almost every address stored in the file system (slist
addresses, tag links, catalog page numbers, the MDDF's own pointers) is
**MDDF-relative**. To get the absolute sector number (index into the image) add the MDDF
sector number:

```
absolute = MDDF_sector_number + relative
```

All sector numbers are 0-based: the very first absolute sector (aka the boot sector) is 0.

#### Tag size and disk kinds

The per-sector tag size is derived from `tagSize / num_sectors` (DC42) or is 20 (raw
images), and tells you the medium:

| Tag size | Medium | Typical sector counts |
|----------|--------|-----------------------|
| 12 bytes | Lisa/Sony floppy, Twiggy | 800 (400K), 1600 (800K), 1702 (Twiggy 872K) |
| 20 bytes | ProFile / hard disk | 9728 (5 MB), 19456 (10 MB), … (e.g. 94208 = 50 MB) |


#### The 20-byte tag (ProFile / hard disk)

| Offset | Size | Field | Meaning |
|--------|------|-------|---------|
| 0  | 2 | `version`  | page version number |
| 2  | 2 | `vol_id`   | volume identifier: Set to 0 on most sectors, except set to e.g. 36 for the boot sector  and for the "loader" sectors. |
| 4  | 2 | `file_id`  | the page's file ID (see §3.3) |
| 6  | 2 | `dataused` | valid bytes in this page's 512 data bytes. The `0x8000` bit is a flag that is set on every tag; the count is `dataused & 0x7FFF`. |
| 8  | 3 | `abs_num`  | absolute sector number (counted from the MDDF sector) |
| 11 | 1 | `checksum` | per-sector checksum (see below) |
| 12 | 2 | `rel_num`  | page's position within its file (0 for the first page) |
| 14 | 3 | `fwd_link` | MDDF-relative next page of the file; `0xFFFFFF` = end of chain |
| 17 | 3 | `bkwd_link`| MDDF-relative previous page |

**Per-sector tag checksum** (byte 11): XOR of all 512 bytes of the sector data, XOR'd
with all 19 other tag bytes (byte 11 itself excluded). Verified against a real hard-disk
image (`lisaem-profile.dc42`): 9728/9728 sectors matched. Whenever the tool modifies a
sector's data in a 20-byte-tag image it must recompute and write this byte.

#### The 12-byte tag (floppy)

Per `FINISH_READ` in Lisa source `SOURCE-SONYASM.TEXT.unix.txt` ("UNPACK THE 12 BYTE
HEADER"):

| Offset | Size | Field | Meaning |
|--------|------|-------|---------|
| 0  | 2 | `version` | page version |
| 2  | 2 | `vol_id`  | volume identifier |
| 4  | 2 | `file_id` | page's file ID |
| 6  | 2 | `rel_num` | page position within its file |
| 8  | 2 | — | high 5 bits: `dataused` (top 5 bits); low 11 bits: `fwd_link` |
| 10 | 2 | — | high 5 bits: `dataused` (low 5 bits); low 11 bits: `bkwd_link` |

So there is **no separate dataused field and no checksum**: the 10-bit `dataused`
(0..512) is split across the top 5 bits of both words, and each 11-bit link is MDDF
relative with `0x07FF` as the "no link / end of chain" sentinel (the driver
sign-extends it to `0xFFFFFFFF`). This layout was verified against every file-data
sector of a real 400K floppy image.

#### File ID types

The 2-byte `file_id` in every tag classifies the sector. Fixed, well-known values
(from DiscImageChef's `LisaFS/Consts.cs` and the Lisa sources):

| File ID | Name | Meaning |
|---------|------|---------|
| `0x0000` | `FILEID_FREE`   | free/unallocated page |
| `0x0001` | `FILEID_MDDF`   | the MDDF sector (Media Descriptor Data File) |
| `0x0002` | `FILEID_BITMAP` | allocation bitmap sector(s) |
| `0x0003` | `FILEID_SRECORD`| slist sector (array of 14-byte s_entry records) |
| `0x0004` | `FILEID_CATALOG`| B-tree catalog node sector |
| `0x7FFF` | `FILEID_ERASED` | erased/blanked page |
| `0xAAAA` | `FILEID_BOOT`   | boot sector (negative: `-0x5556`) |
| `0xBBBB` | `FILEID_LOADER` | boot loader sector (negative: `-0x4445`) |

Anything else is a **regular file** page, and the value encodes *which* file:

* **data pages of file N** are tagged with `+N` (the s-file id);
* **hint page of file N** (the page holding the hentry, see §5.8) is tagged with
  **`−N`**, i.e. `0x10000 − N`. That is why real hint-sector file IDs look like
  `0xFFBD`, `0xFFE4`, `0xFFF9` — they are small negative numbers and always start with
  `0xFF`. Because of this, hint sectors cannot be found by a fixed file-ID scan; you
  must know the s-file id.
  
### 5.1 The allocation bitmap

The bitmap (`FILEID_BITMAP` = `0x0002`) has **one bit per sector**, with bit *i*
covering sector `MDDF_sector + i` (i.e. MDDF-relative numbering). It seems that LOS uses this data to quickly find free sectors, when needed.
It starts at
MDDF-relative page `bitmap_addr` (normally 1) and spans `bitmap_pages` sectors; each
bitmap sector covers 4096 sectors (512 bytes × 8 bits). 
A **set** bit means "allocated",
a clear bit means "free". The tool's `is_sector_free_in_bitmap()`, `dump_bitmap_sectors()`
and `check_bitmap_for_all_file_data()` use it to verify that every sector of every
file is actually marked allocated.

### 5.2 The slist — s_entry records (S-record sectors)

Every file on the volume has an **s-file number** (s_file_id, a 16-bit unique file id), and every
s-file has one 14-byte **s_entry** record stored in the volume's **slist** (sectors
tagged `FILEID_SRECORD` = `0x0003`). The 14 bytes are:

* `hintaddr` — 4 bytes: MDDF-relative page of the file's **hint sector** (the one containing the
  hentry; §5.8). 0 (or REDLIGHT = −1) = none.
* `fileaddr` — 4 bytes: MDDF-relative page where the file's **data** begins. 0 = empty file.
* `filesize` — 4 bytes: logical size of the file in bytes.
* `version`  — 2 bytes: file version (typically 1).

The slist table starts at MDDF-relative page `slist_addr` and holds `slist_packing` (usually
36) records per slist sector (36*14=504 bytes, which fits nicely in a 512-byte sector, the rest 8 bytes are unused). The record for s-file *N* is at:

```
page   = slist_addr + (N div slist_packing)
offset = (N mod slist_packing) * 14
```

Notes:

* The first few s-records can contain garbage (huge, e.g. `0xFFFFFF3D` hint addresses);
  the tool skips records whose hint sector number falls outside the image.
* On fs 14/15 volumes the scan range is s-files `1 .. empty_file − 1`
  (`_flat_catalog_sfile_range()`); unused slots have `hintaddr = 0`.
* **The slist can go stale.** If a volume is damaged (or a file was moved by tools that
  don't maintain the slist), `hintaddr` may point at a sector that is *not* this file's
  hint page — e.g. another file's data page or a free page — and reading it as an hentry
  yields a garbage name and a bogus `protected` flag. The tool detects this via the tag
  (a hint page must be tagged `−s_file_id` with `rel_num = 0`; §5.8) and, if needed,
  rescans all sector tags for the real hint page (`_locate_hint_page_for_sfile()`).

### 5.3 The catalog — B-tree version (used on fs_version 16 and 17)

The Catalog is an on-disk data structure that can be used by LOS to quickly locate a file, given a file name.
To do so, the structure is being maintained by LOS to be always ordered by file name alphabetically.

LOS 1.0 and 2.0 have a "flat" catalog data structure. From LOS 3.0 on, the catalog is an **HFS-style B-tree** (with a max depth of 3).

All catalog sectors are tagged as `FILEID_CATALOG` (have file_id=`0x0004`). The root page is the MDDF-relative page number stored at
MDDF offset `0x12E` (`root_page`). (Scanning tags for `0x0004` is *not* reliable: on some
volumes the pointer in `0x12E` is the only trustworthy way in.)

Below we describe the B-tree catalog (used on fs_version 16 and 17)

**Node layout.** The catalog consists of "nodes". A node is exactly 2048 bytes = **4 consecutive sectors**:

* Records start at offset 0 and grow toward the end of the node.
* The **offset table** holds the start offset (2-byte big-endian) of record *i* at node
  offset `2034 − 2·i`. The entry at index `nkeys` (i.e. at `2034 − 2·nkeys`) is the
  "used" sentinel = end offset of the last record.
* The **node descriptor** at offset 2036:

| Node offset | Size | Field | Meaning |
|-------------|------|-------|---------|
| 2036 | 2 | `nkeys` | number of records in the node |
| 2038 | 4 | `prior` | MDDF-relative previous node |
| 2042 | 4 | `next`  | MDDF-relative next node; `0xFFFFFFFF` (BAD) = end of leaf chain |
| 2046 | 1 | `kind`  | 0 = leaf node, 1 = index node |
| 2047 | 1 | `cksum` | node checksum |

* **Key** (36 bytes, `MakeKey` in `source-fsasm.text.unix.txt`):
  `[0x24][ parent ID : u16 ][ name : up to 32 bytes, zero padded ][ 0x00 ]`.
* **Index node records**: `[ child page : u32 (MDDF-relative) ][ key : 36 bytes ]`.
* **Leaf node records**: `[ key : 36 bytes ][ eType : u16 ][ type-specific fields ]`.
  In every image examined, the entry type is in the **high byte** of `eType`
  (e.g. fileentry = `0x0300`); the low byte holds extra per-entry info.

**Entry types** (high byte): `0` empty, `1` directory, `2` link, `3` file, `4` pipe,
`5` ec, `6` killed, `7` removed, `8` thread.

| Record | Total size | Fields after the 36-byte key |
|--------|-----------|------------------------------|
| **FILEENTRY** | 64 | `eType(2)`, `sfile(2)` @38, `fileDTC(4)` @40, `fileDTM(4)` @44, `size(4)` @48, `physSize(4)` @52 (size rounded up to 512), `fsOvrhd(2)` @56, `flags(2)` @58, `fileUnused(4)` @60 |
| **DIRENTRY**  | 48 | `eType(2)`, `dir_id(2)` @38, `dir_dtc(4)` @40 |
| **THREADENTRY** | 78 | `eType(2)`, `parID(2)` @38, `myName` (34-byte Pascal string) @40 |

Important: a **FILEENTRY does not contain the hint sector number** — only the
`s_file_id`. The hint sector is derived by looking into the slist data for the file (§5.2,
`get_sentry_for_sfile()`).

**Traversal** (mirrors `FirstRec`/`SeqRec` in `source-fsdir.text.unix.txt`):

1. If the root is an index node, descend to the **leftmost child** (the child page of
   the first record) until a leaf is reached.
2. Enumerate all records of the leaf via the offset table, then follow the leaf's
   `next` pointer until it is BAD (`0xFFFFFFFF`).
3. Works at any B-tree depth (the OS's `NodeStack` is `array[0..15]`; the MDDF's
   `tree_depth` is a 2-byte field), but in reality at-most depth of 3 is being used.

The first record of the catalog is the "thread entry" for `''` (parent id 0) — the
**root directory**. Every directory on the volume has exactly one THREADENTRY.

### 5.4 The catalog — flat version (used on fs_version 14 and 15, aka "Flat_Catalog")

Volumes with `fsversion ≤ 15` have **no B-tree catalog**. Instead, the root catalog is a
*regular file* (the s-file whose hentry `ftype` is `rootcat = 2`; on such volumes it is
the s-file `first_file` from the MDDF — the tool locates it by scanning the slist for
robustness). Its data is a fixed array of `rootmaxentries` (== `maxfiles`) **54-byte
centry records** — a hashed table with linear probing. The rootcatalog itself has no
catalog entry.

```
centry (54 bytes; enums are 1 byte on disk in this version):
  0x00  name         : e_name      (34 bytes: 1 length byte + 33 chars)
  0x22  cetype       : entrytype   (1 byte)
  0x23  (pad byte)
  0x24  sfile        : integer
  0x26  attributes   : longint     (reserved for future use)
  0x2A  readpage     : longint     (pipe entries only)
  0x2E  readoffset   : integer
  0x30  writepage    : longint     (pipe entries only)
  0x34  writeoffset  : integer
```

`cetype`: `0` emptyentry, `1` direntry, `2` linkentry, `3` fileentry, `4` pipeentry,
`5` ecentry, `6` killedentry, `7` removed, `8` threadentry.

The slot of an entry is `hash(UPPERCASE(name)) mod rootmaxentries`, where (from
`LOOKUP_BY_ENAME` in `source-fsprim.text.unix.txt`; Pascal strings are 1-based):

```
temp = ord(c1) * (ord(clast) + 1)
for m = l-2 downto 1:  temp += ord(c_{m+1}) * (ord(c_{m+2}) + 1)
slot = abs(temp) mod rootmaxentries
```

with **linear probing** for collisions. `flat_catalog_dump_catalog()` re-verifies each
entry's slot against this hash and cross-checks each fileentry's s-file against the
slist (if there is a file name mismatch between the catalog and the hint entry, then the slist is stale).

### 5.5 Reading a file's data

Both catalog versions (Flat or B-Tree) converge on the same mechanics, driven by the slist:

1. Start at the file's first data page: `absolute = MDDF_sector + fileaddr`.
2. Read the page's data — only the first `dataused` bytes are valid (in 20-byte tags
   mask off the `0x8000` flag bit).
3. Follow the tag's `fwd_link` (MDDF-relative) to the next data page.
4. Stop at `filesize` bytes total, or at the end-of-chain special `fwd_link`
   (`0xFFFFFF` in 20-byte tags, `0x07FF` in 12-byte tags).
5. `fileaddr = 0` or `filesize = 0` ⇒ the file is empty.

All data pages of file *N* are tagged `+N`, so an alternative way is to find all sectors whose tag carries the file's id, sort them and read them.

The `dump` commands reads all files (into folder /tmp/LisaFileSystemDump). Text files (with file names ending with ".text" ) are dumped as plain host text: the 1024-byte on-disk header page and the null page padding are stripped, and the \r new-line symbols used by LOS are converted to \n, so no further conversion is needed.

### 5.6 File names

Files are identified by a Pascal-string name (up to 33 characters in fs versions 14/15,
`e_name = string[33]`; 32 characters + 1 pad byte in fs 16/17 — either way the name
field occupies 34 bytes). The name lives both in the **hentry** (§5.8) and in the catalog
key (§5.3/§5.4). Names are decoded using "Mac Roman" encoding in this tool (it is unclear how well it works with non-english languages).

### 5.7 Filetype enum

The hentry's `ftype` field (see `LISA_OS/OS/source-sfileio.text.unix.txt`):

| Value | Name | Value | Name |
|-------|------|-------|------|
| 1  | MDDFfile    | 9  | pipe |
| 2  | rootcat     | 10 | bootfile |
| 3  | freelist    | 11 | swapdata |
| 4  | badblocks   | 12 | swapcode |
| 5  | sysdata     | 13 | ramap |
| 6  | spool       | 14 | **userfile** |
| 7  | exec        | 15 | killedobject |
| 8  | userdir     | 16 | tempfile |

### 5.8 Hint sectors and the hentry record

Each file has one **hint sector** (a data structure describing and pointing to the file's
data). The hint sector begins with the **hentry** record (file header, defined
in `LISA_OS/OS/source-fsprim.text.unix.txt`). The OS allocates hint pages with a
*negative* file id — `appendpages(..., -free, ...)` in `NEW_SFILE`,
`source-sfileio1.text.unix.txt` — so:

* hint page  of file *N* have a tag with `file_id = −N` (`0x10000 − N`), `rel_num = 0` holds the hentry;
* data pages of file *N* have a tag with `file_id = +N`.

**hentry layout** (offsets within the hint sector; verified against real images):

| Offset | Size | Field | Meaning |
|--------|------|-------|---------|
| 0x00 | 34 | `name` | e_name Pascal string (34 bytes in all versions; see §5.6) |
| — | 1 | (gap) | the fields are *not* tightly packed |
| 0x22 | 8 | `unique_ID` | UID = two 32-bit longs (a, b) |
| 0x2A | 2 | `version` | file format version; **21 = `cur_file_version`** |
| 0x2C | 1 | `ftype` | filetype enum (§5.7). One byte in all versions; the byte at 0x2D is pad (uninitialized junk on fs 14/15, since the old hentry is only 88 bytes and was written without ClearMem) |
| 0x2E | 4 | `DTC` | created |
| 0x32 | 4 | `DTA` | accessed |
| 0x36 | 4 | `DTM` | modified |
| 0x3A | 4 | `DTB` | backup |
| 0x3E | 4 | `DTS` | scavenged |
| 0x42 | 4 | `machine_id` | machine the file may be opened on — **theft protection** (§5.9) |
| 0x46 | 1 | `killed` | boolean flag |
| 0x47 | 1 | `safety_on` | boolean flag |
| 0x48 | 1 | **`protected`** | boolean flag (theft protection) |
| 0x49 | 1 | `master` | boolean flag |
| 0x4A | 1 | `scavenged` | boolean flag |
| 0x4B | 1 | `closed_by_OS` | boolean flag |
| 0x4C | 1 | `file_open` | boolean flag |
| 0x4D | 2 | `result_scavenge` | integer |
| 0x4F | 2 | `unusedi1` | integer |
| 0x51 | 2 | `system_type` | integer |
| 0x53 | 2 | `user_type` | integer |
| 0x55 | 2 | `user_subtype` | integer |

**fs version 14/15 only ends here**: the old hentry is **88 bytes** (through 0x57). The
following fields exist only in fs version 16/17 (hentry = 112 bytes, ending at 0x6F):

| Offset | Size | Field | Meaning |
|--------|------|-------|---------|
| 0x57 | 8 | `build_info` | Build_Control = 4 × integer: release @0x57, build @0x59, compat @0x5B, revision @0x5D |
| 0x5F | 2 | `file_portion` | integer |
| 0x61 | 9 | `password` | Str8 Pascal string (file password) |
| 0x6A | 4 | `parentID` | NodeIdent of the parent directory |
| 0x6E | 2 | `fsOverhead` | per-file system overhead |

**Small file map** at offset **0x80** of the hint sector (the "smallmap"):

```
0x80  size        : u32  — number of PAGES in the file (GET_PSIZE = size × pgdatasize)
0x84  max_entries : u16  — usually 9
0x86  ecount      : u16  — entries currently in use
0x88  map[0..9]   : 10 × ( address : u32 (MDDF-relative), cpages : u16 )
```

The smallmap is a **display optimization only** — to actually read the file you follow
the tag `fwd_link` chain from the slist's `fileaddr`. It can go stale after a file is
shrunk.

### 5.9 Protected files (theft protection) and removing the protection (deserialization)

Now the fun part: how disk protection works in LOS.

LOS uses DRM (digital rights management) (aka "serialization") when copying some of its tools (e.g. LisaDraw) from an installation floppy disk onto a hard drive,
to prevent you using that floppy disk onto multiple Lisas. How it works: when you copy an app (e.g. LisaDraw) from a floppy disk to your Lisa's hard drive for the very first time, the Lisa will prompt you that you're about to serialize that disk to your Lisa, and if you choose to proceed, that app will work only on this Lisa. What happens is: LOS will write your Lisa's "AppleNet" number to both the file on the floppy disk and the file on your hard drive. Now, if you try to install (copy) the file from this floppy onto another Lisa, it will do it, but, when you try to launch the file, it will refuse to launch it, because the file was "serialized" on another Lisa. Similarly, if you connect your ProFile hard disk to another Lisa and try to run LisaDraw from it, you will get the same error.

It seems that all Lisa tools in LOS 1.0 and 2.0 are protected, even some Workshop files (e.g. Pascal.obj - this is the Pascal compiler, and even the Editor app in Editor.obj). 
However, Apple chose to protect only certain tools in LOS 3.0 and 3.1 (e.g. LisaDraw is protected, LisaWrite is not). 

Note that the whole disk protection framework was (and is) very easy to bypass, if you were aware of it: Just make a fresh copy of each original floppy disk image, use that copy to install whatever you are installing, and then either wipe those floppies, or lablel them "serialized to Lisa AppleNet .....".

The Lisa's "AppleNet" number (e.g. 00102905) is a 4-byte chunk stored along with (as part of) the Lisa's serial number, in the "Video ROM", on the CPU board, and can be also found under the front panel of your Lisa.

Where, on disk, is the file protection stored? Each file has a "hint sector number", which, among other fields, contains a **`protected`**  byte (at offset 0x48) along with a
**`machine_id`** (4 bytes at offset 0x42).

The "deserialize" command scans all files on the disk image, finds the ones whose **`protected`**  byte is set, and resets it to zero, and also writes "all-zeroes" in the 4-bytes **`machine_id`**. After that, the file will no-longer be protected, so it can be copied-to and run just anywhere (on any Lisa).

How does a "virgin" (never used) LOS floppy disk look like? Consider the LisaDraw 3.1 installation floppy disk. It contains one protected file named '{T4}obj' (the LisaDraw executable) with machine_id:0 (0x00000000) = AppleNet '00000000'. When copying the LisaDraw from from the floppy to your hard disk (via the usual "duplicate then drag"), the special `machine_id:0 + protected:0` is what prompts LOS to pop the message `The Lisa is about to make the first copy of LisaDraw. Afterwards, this copy, and all future copies, can be used only on this Lisa. Is this what you want?`. Once you complete copying the file, both files (the one on the floppy disk and the one on your hard disk) will be updated to contain your Lisa's AppleNet id. And this is why the LisaEm emulator has an AppleNet id of 0 (see it in File->Preferences) : when this file is being copied on LisaEm, LOS will update the file's AppleNet id from 0 to 0, which basically leaves the disk virgin, ready to be used on any Lisa. [Clever](https://lisalist2.com/index.php?topic=65.0)! 

Note: In prior literature (by others), "deserialization" meant "reset the machine_id of a protected file to 0". Here it means "reset the machine_id of a protected file to 0 and set the protected flag to 0", which basically turns a protected file into a regular LOS file.

More technical details:

* The machine_id is also stored in the MDDF sector: the `init_machine_id` field is populated during the LOS or Workshop installation. The `master_machine_id` field is typically 0; I think it gets populated when you copy/backup/duplicate a ProiFile disk or a floppy disk, at which point the fields `DT_copied`, `DT_copy_created` also get populated. The `deserialize` command does not touch these fields; it seems that they are stored there by LOS for historical record keeping only.

* The "open file" code path (GOPEN,
`LISA_OS/OS/source-fsui1.text.unix.txt`) allows the file to be opened only if the file
is `master`, or is not protected, or `machine_id` equals the machine's AppleNet id.
Otherwise the open fails with **error 142: "<file name> is a protected file"**.

Per `source-SERNUM.TEXT.unix.txt`, `machine_id = first3 × 65536 + last5`, where
`first3`/`last5` are the BCD digits of the 8-digit AppleNet serial number — so the tool
can convert a machine_id to an AppleNet id, e.g. `machine_id 67624 (0x00010828) = AppleNet '00102088'`.

The tool's handling:

* `find_protected_files()` / `_find_protected_files_flat_catalog()` — walk the catalog:
    - for B-tree catalogs: finds every FILEENTRY → slist → hint sector); 
    - Flat: every slist entry → hint sector)

  , then read the `protected` byte at 0x48 in the hint sector, and report the file name, s_file_id, machine id and hint
  sector number of every protected file.
* `remove_file_protection()` (command `deserialize`) — after an interactive
  confirmation prompt, for each protected file it rewrites exactly 5 bytes of the hint
  sector in the image: `machine_id = 0x00000000` (offset 0x42, 4 bytes) and
  `protected = 0` (offset 0x48, 1 byte). With both cleared, GOPEN passes on any
  machine. Because sector data changed, it then:
  * for 20-byte tags: recomputes and writes the **per-sector tag checksum** (tag byte
    11, §3.2) of the affected hint sector; (12-byte floppy tags have no checksum field —
    nothing to do);
  * for DC42 images: recomputes and writes the **`dataChecksum`** (header offset 0x48)
    and **`tagChecksum`** (offset 0x4C) in the DC42 header.
  
  Every write is read back and verified; a mismatch prints a WARNING.

---

## 6. Conventions and small utilities

### 6.1 Pascal strings

Length-prefixed strings: 1 length byte followed by the characters. The tool decodes
with Mac Roman (`pascal_to_string()`), stopping early at an embedded NUL or if the
length byte is bogus.

### 6.2 Endianness

Everything on disk (DC42 header, tags, MDDF, slist, catalog, hentry) is **big-endian**.
The tool's `to_uint16/32_big_endian()` helpers do the decoding.

### 6.3 Dates

Lisa stores dates on the file system in the DT_* fields in the MDDF sector, and in each file's hint/hentry DTC/DTA/DTM/DTB/DTS fields.
These fields are 4-bytes long.

A Lisa timestamp is the number of **seconds since the midnight prior to 1 January
1901** (not 1900!), in GMT (`libhw-TIMERS.TEXT.unix.txt`; `baseyear = 1901` in
`source-TIMEMGR.TEXT.unix.txt`). The OS kept times in GMT and converted to local time
only for display. 
  * This needs more investigation: how is "the system's local timezone" edited and where is it stored on disk?
    It is possible that the keyboard layout (e.g. French) is used to determine the the system's local timezone?

The tool's `format_date()` function assumes that the date was stored in GMT/UTC timezone, so it subtracts 2177452800 (the 1901→1970 offset) and
formats it (converts it) in the host's local time zone. A value of `0` means "never set/undefined".

How do you set the clock in LOS? Answer: in a non-intuitive way: Launch the "Clock" application, select e.g. the year field, and type a new value on the keyboard. There is no option for setting the user's time zone.

The maximum possible date is: 2037-02-06 06:28:15 GMT (i.e. value 0xFFFFFFFF = 4,294,967,295 seconds), but the "Clock" does not let you enter any year outside of 81..95, so the maximum date you can enter is 1995-12-31. Online articles suggest that LOS rolls over its clock back to 1980-01-01 after passing 1995-12-31. Whyyyy! Now that the Lisa sources are available, this can be fixed to allow modern dates, but still, the "2037-02-06 06:28:15" doomsday will inevitably come.

---

## 7. Method map (what does what)

| Method | Purpose |
|--------|---------|
| `InMemoryFileSystem.__init__` | Load the image in memory, detect DC42 vs raw, validate sizes and checksums, locate the MDDF (boot sector first, tag scan as fallback), read `fsversion` (must be 14..17). |
| `print_extra_info` / `print_mddf_sector_info` | `info` command: MDDF fields + list of sectors per file-ID type. |
| `print_sector_tags` / `pretty_print_tags_for_sector` | Hex/annotated dump of raw tag bytes. |
| `print_hint_sector_info(n)` | Full annotated decode of one hint sector (hentry + smallmap + protection summary). |
| `dump_catalog` | Walk the B-tree catalog and print every record (files/directories/threads). |
| `print_catalog_record` | Decode one catalog record; returns its category. |
| `find_catalog_root_page_sector_number` | MDDF offset 0x12E → catalog root page. |
| `flat_catalog_list_files` / `flat_catalog_dump_catalog` | fs 14/15: list files from the slist; dump & verify the hashed rootcatalog. |
| `flat_catalog_read_file_data` | Read a whole file via its tag chain, honoring `dataused` and `filesize`. |
| `_find_rootcatalog_sfile` | Find the s-file whose hentry `ftype` is `rootcat` (2). |
| `flat_catalog_hash` | Reimplementation of the OS's catalog hash. |
| `dump_files` / `_dump_files_flat_catalog` | `dump` command: write all files to folder `/tmp/LisaFileSystemDump/`. |
| `get_sentry_for_sfile` | s_file_id → (hint sector, data start sector) via the slist. |
| `_locate_hint_page_for_sfile` | Validate/relocate a (possibly stale) hintaddr using the tag (`file_id = −s_file_id`, `rel_num = 0`). |
| `find_protected_files` / `_find_protected_files_flat_catalog` | List all theft-protected files. |
| `remove_file_protection` | `deserialize` command: clear `machine_id`/`protected`, fix tag + DC42 checksums. |
| `is_sector_free_in_bitmap` / `dump_bitmap_sectors` / `dump_free_bitmap_sectors_numbers` / `check_bitmap_for_all_file_data` | Allocation-bitmap inspection and consistency checks. |
| `derive_mddf_sector_number_from_boot_sector_zero` | Reproduce the boot ROM/loader MDDF derivation (§3.5). |

---

## 8. Caveats and known quirks

* The project name `LisaFileSystemTool` implies that there is only one file system used on the Apple Lisa computers. We know this is not true. The name was chosen for simplicity. The tool works only with disk image files for Lisa Office System (aka LOS) and Workshop.
* **MDDF offsets**: the published Pascal record (`SOURCE-VMSTUFF.TEXT.unix.txt`) does
  not line up perfectly with real Lisa profile disk images; the offsets actually used
  here were corrected empirically. Fields marked "verified correct" in
  `MDDF_FIELD_DEFINITIONS` are the load-bearing ones.
* **DC42 tag checksum skips the first tag** — a quirk inherited from the reference
  implementation; both verification and `deserialize` honour it.
* **Raw-image detection is a heuristic** (no `0x0100` at offset 0x52 ⇒ raw).
* **Stale slist entries** occur on some real images (e.g. Twiggy file `LOS1.01.dc42` found on the webs); the
  tool recovers by rescanning tags, but a warning is printed.
* **Stale smallmaps** can occur after a file is shrunk — use the tag chain, not the
  smallmap, for data.
* The output directory `/tmp/LisaFileSystemDump/` is hard-coded; filenames are used verbatim.
* `deserialize` needs an interactive terminal for the confirmation prompt; without one it
  aborts safely.
* The whole image must fit in memory (and be ≤ 100 MB).


---

## 9. FAQ

* Can this tool remove password protections? Answer: No. Long answer: LOS allows setting up a password for specific files (select the file's icon, then use the File->Attributes of ..." menu to set/remove a password). The password is being encrypted and stored in the "hint sector" of the file and it can be printed by this too (see function `print_hint_sector_info()`). The `deserialize` tool command does not deal with these files, but this feature could be added. 

* Let's say LOS was installed on a ProiFile hard drive attached at the lower port on a dual-port paralel card in Slot 1. It his information stored somewhere on the disk volume? 
Answer: yes, it seems that the boot device number is stored in the tag of the boot sector 0, field vol_id, and also in the tags of all "loader" sectors, same field, same values. All other sectors have a vol_id=0. See LOS sources file SOURCE-FSINIT1.TEXT.unix.txt : the possible values are 0..39. What this means: if you attach a disk image on another slot / paralel port (from the one it was installed on), it may fail to boot (as we have seen "in the field").

---

## 10. References

* Lisa Operating System Reference Manual, Mar 1982
  (<https://bitsavers.org/pdf/apple/lisa/os/Lisa_Operating_System_Reference_Manual_Mar82.pdf>) —
  pages/labels, MDDF, bit map, catalog, file hints.
* Lisa OS source files at https://info.computerhistory.org/apple-lisa-code:
  `SOURCE-VMSTUFF.TEXT.unix.txt` (MDDF record), `source-fsprim.text.unix.txt` (hentry,
  centry, flat catalog, hash), `source-sfileio.text.unix.txt` (s_entry, cur_file_version,
  filetype), `source-fsui1.text.unix.txt` (GOPEN theft-protection check),
  `source-SERNUM.TEXT.unix.txt` (machine_id), `source-fsdir.text.unix.txt` and
  `source-fsasm.text.unix.txt` (B-tree catalog, keys), `source-fsinit.text.unix.txt` /
  `source-ldlfs.text.unix.txt` (slist location), `SOURCE-SONYASM.TEXT.unix.txt`
  (12-byte tag unpacking), `source-LDEQU.TEXT.unix.txt` / `SOURCE-FSINIT1.TEXT.unix.txt`
  (boot sector, PWBT), `source-LDPROF.TEXT.unix.txt` / `source-LDTWIG.TEXT.unix.txt`
  / `source-ldmicro.text.unix.txt` (vol_starts), `source-sfileio1.text.unix.txt`
  (NEW_SFILE), `source-scavenger.text.unix.txt`, `libhw-TIMERS.TEXT.unix.txt` /
  `source-TIMEMGR.TEXT.unix.txt` / `source-clock.text.unix.txt` (dates).
* DiscFerret wiki, *Apple DiskCopy 4.2* — DC42 format
  (<https://www.discferret.com/wiki/Apple_DiskCopy_4.2>).
* Lisa serial number format at https://lisalist2.com/index.php?topic=313.0
* stepleton/bootloader `dc42_build_bootable_disk.py` — DC42 checksum algorithm
* Lisa Computer Tool Deserialization Documents by David Craig at http://www.applerepairmanuals.com/lisa/deserial/pg02.html
  (<https://github.com/stepleton/bootloader>).
* LOS/Workshop text file format at https://www.bitsavers.org/pdf/apple/lisa/toolkit_3.0/Package_2_Examples/17_Lisa_Development_System_Internals_Documentation_Feb84.pdf
  , pages 37 and 38.

## 11. Relevant  / similar tools:

* The tool at https://github.com/alexthecat123/LOSSerialTool/blob/main/LOSSerialTool.py can deserialize floppy disk images (only). Its "-deserialize -clearbozo" mode does the same as the "deserialize" command of my tool: the -deserialize options clears the machine_id at offset 0x42 in the hint sector of the protecxted file, and the -clearbozo option clears the "protected" flag at offset 0x48 in the same hint sector. Unlike my tool, it does not scan the catalog to list all files, but instead looks for specific bytes in the file to try to locate each "hint sector".

* The tool at https://github.com/arcanebyte/lisaem/blob/master/src/tools/src/lisafsh-tool.c can list and dump files from a DC42 disk image. It was written by Ray Arachelian (the LisaEm author) long before the LOS source files were open-sources, which was an impressive achievement.

* The tools at https://github.com/tfrikker/lisa_utils/tree/master/srcBuilder can list and dump files from a DC42 disk image. There is also an attempt to implement "add file", but it fails for me. Perhaps I did not try hard enough...

## 12. Disclaimer

I used AI in this project, to write some of the code and parts of this doc.

## 13. License

Published under the GNU General Public License v3.0.
