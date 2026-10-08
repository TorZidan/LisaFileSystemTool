#!/usr/bin/env python3
"""
Prints the shared (also known as 'intrinsic') libraries used by a Lisa executable file (present on the given disk image).

Usage: 
    python3 AnalyzeLisaExecutableFile.py <disk image file name> <name of executable .OBJ file>

It reads the specified executable file (e.g. EDITOR.OBJ) and the INTRINSIC.LIB
file from the specified  Lisa disk image file.
The name of the INTRINSIC.LIB file is hard-coded (INTRINSIC_LIB_NAME).

The parsing operates on the files' contents as plain `bytes` objects
(file_as_bytes), not on host file names, so switching back to ordinary
files on the computer's file system is a one-line change: replace the
read_file_from_disk_image() calls in analyze() with
`open(host_file_name, "rb").read()`.

Note: Some of this info can be printed by the Workshop utility DUMPOBJ.OBJ.

Note: The full OBJ file binary format is detailed at
https://bitsavers.org/pdf/apple/lisa/workshop_3.0/Lisa_Develpment_System_Internals_Documentation_198402.pdf
page 102 .. 141. This utility was written without relying on that information, so it may be wrong. 

Background
----------
A Workshop / Lisa Pascal program does not contain the code of the intrinsic
units it uses.  It is dynamically linked against the shared intrinsic
library (INTRINSIC.LIB and its companion library files).  The object file
therefore holds two reference tables:

  * a *unit table*    -- the Pascal units the program references; the
    Pascal runtime uses it to resolve unit numbers, and
  * a *segment table* -- the named code segments the OS loader must pull
    from the shared library at load time.

A single unit can be made up of several segments (e.g. FPLIB is backed by
the segments initfp, fplib2, fpmodes, f32, x80, x80elem and fptrap), so the
two tables are related but not one-to-one.

File layouts
------------
INTRINSIC.LIB
  0x00   99 00             magic
  ...    header
  0x22   unit directory, 16-byte records:
             8   unit name, padded with spaces
             2   unit number
             1   lib_file_id
             1   flag
             4   size / extra
  0x5A2  9c 00             segment-library marker
  0x5A8  segment directory, 28-byte records:
             8   segment name, padded with spaces
             2   segment number
             8   descriptor
             10  extra (byte 1 = lib_file_id)
  ...    9e 00             lib_file_id -> library file-name table:
             2   value (unused)
             2   entry count
             6 * count:
                   2   lib_file_id
                   4   offset of a Pascal-string file name

Unit table in an .OBJ file
  0x1C   9b 00             unit-table marker (searched for anywhere,
                           because linked files embed it deeper)
  0x1E   16-bit value      (not used)
  0x20   16-bit entry count
  0x22   16-bit highest unit number
  0x24   entries, 12 bytes each:
             8   unit name, appears in upper case, e.g. "PASLIB"
             2   unit number, e.g. 1
             2   unit_type, e.g. 1 means "Intrinsic"

Segment table in an .OBJ file
  A contiguous run of 18-byte entries:
             8   segment name
             2   segment number
             8   descriptor
  The run is located by scanning for stretches of valid entries; a stretch
  that contains PASLIB1 (segment 17, present in virtually every program)
  is preferred, then the longest.
"""

import struct
import sys

from LisaFileSystemTool import fixed_len_bytes_to_string, pascal_to_string
from LisaFileSystemToolPerFile import FileSystemWithPerFileCommands

OBJ_UNIT_TABLE_MARKER = b"\x9b\x00"
SEG_LIB_MARKER = b"\x9c\x00"

# The name of the intrinsic library file on the volume (hard-coded).
INTRINSIC_LIB_NAME = "INTRINSIC.LIB"

# Unit names are short uppercase identifiers in a fixed-length,
# space-padded field.
NAME_CHARS = set(b"ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_ ")

# Segment names may be mixed case.
SEG_NAME_CHARS = set(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_")

# Meaning of the 2-byte unit_type field in the unit table of an .OBJ file.
UNIT_TYPE_NAMES = {0: "Regular", 1: "Intrinsic", 2: "Shared"}


def unit_type_name(unit_type: int) -> str:
    """Human-readable name for the unit_type field:
    0=Regular, 1=Intrinsic, 2=Shared, everything else=Unknown.
    """
    return UNIT_TYPE_NAMES.get(unit_type, "Unknown")


def strip_volume_prefix(name: str) -> str:
    """Strip a LisaFiling volume prefix like '-#BOOT-' from a file name.

    A name of the form '-#<volume>-<file>' refers to <file> on the named
    volume (here: the boot volume); the prefix is not part of the file's
    name on that volume, so it is dropped.
    """
    if name.startswith("-#"):
        end = name.find("-", 2)
        if end > 2:
            return name[end + 1 :]
    return name


def is_plausible_name(raw: bytes) -> bool:
    """True if raw looks like a unit-name field (no NULs, uppercase-ish)."""
    if not raw:
        return False
    if any(b == 0 or b not in NAME_CHARS for b in raw):
        return False
    return any(b != 0x20 for b in raw)


def is_plausible_seg_name(raw: bytes) -> bool:
    """
    True if raw looks like a segment-name field.

    Segment names are left-justified, space-padded, mixed-case
    identifiers: they start with a real character and, once a padding
    space appears, every following byte is a space.  This rejects the
    runs of ordinary English text that would otherwise look like names.
    """
    if not raw or len(raw) != 8:
        return False
    if raw[0] == 0x20 or raw[0] == 0:
        return False
    seen_space = False
    for b in raw:
        if b == 0:
            return False
        if b == 0x20:
            seen_space = True
        elif seen_space:
            return False
        elif b not in SEG_NAME_CHARS:
            return False
    return True


def find_unit_table(data: bytes) -> tuple[int | None, int | None]:
    """
    Locate and validate the unit table.

    Returns (marker_offset, count) or (None, None).  A candidate is
    accepted only if its entry count is plausible and every entry has
    a valid name field and a unit number in range, which rules out
    random 9b 00 byte pairs in code or data.
    """

    idx = 0
    while True:
        marker = data.find(OBJ_UNIT_TABLE_MARKER, idx)
        if marker < 0:
            return None, None
        idx = marker + 2

        if marker + 10 > len(data):
            continue
        count = struct.unpack(">H", data[marker + 4 : marker + 6])[0]
        table_start = marker + 8
        if count == 0 or table_start + count * 12 > len(data):
            continue

        ok = True
        for i in range(count):
            off = table_start + i * 12
            if not is_plausible_name(data[off : off + 8]):
                ok = False
                break
            unit = struct.unpack(">H", data[off + 8 : off + 10])[0] & 0x1FFF
            if unit > 255:
                ok = False
                break
        if ok:
            return marker, count

    return None, None


def _seg_entry_ok(data: bytes, off: int) -> bool:
    """True if data[off:off+18] looks like one segment-table entry."""
    if off + 18 > len(data):
        return False
    if not is_plausible_seg_name(data[off : off + 8]):
        return False
    num = struct.unpack(">H", data[off + 8 : off + 10])[0]
    return (num & 0x1FFF) <= 255


def _read_seg_run(data: bytes, start: int) -> list[tuple[int, str, int, bytes, bytes]]:
    """Read consecutive segment-table entries starting at start.

    Each entry is (offset, name, segment_number, descriptor, raw), where
    raw is the entry's complete 18 bytes.
    """
    entries = []
    off = start
    while _seg_entry_ok(data, off):
        name = fixed_len_bytes_to_string(data[off : off + 8])
        num = struct.unpack(">H", data[off + 8 : off + 10])[0]
        desc = data[off + 10 : off + 18]
        entries.append((off, name, num, desc, data[off : off + 18]))
        off += 18
    return entries


def find_segment_table(data: bytes) -> tuple[int | None, list[tuple[int, str, int, bytes, bytes]]]:
    """
    Locate the program's segment table in the object file.

    The table is a contiguous run of 18-byte entries:
        8 bytes:  segment name, space-padded (mixed case)
        2 bytes:  segment number
        8 bytes:  "descriptor", which the caller may split further into so-called
                  "Version1" and "Version12" fields, as reported by the DUMPOBJ.OBJ Workshop utility.

    The file carries no marker for the table, so candidate runs are
    found by scanning for stretches of at least two entries that each
    pass _seg_entry_ok() (plausible name + segment number in range).
    When several candidates exist, the one containing PASLIB1 (segment
    17, present in virtually every program) is preferred; otherwise
    the longest run wins.

    Returns (start_offset, entries), where entries is a list of one
    (offset, name, segment_number, descriptor, raw) tuple per entry, with
    the descriptor being the raw 8 bytes of the entry's last field and raw
    the entry's complete 18 bytes.
    Returns (None, []) if no candidate run is found.
    """
    n = len(data)
    candidates = []
    i = 0
    while i + 18 <= n:
        if _seg_entry_ok(data, i):
            j = i
            while _seg_entry_ok(data, j):
                j += 18
            if (j - i) // 18 >= 2:
                candidates.append(i)
            i = j
        else:
            i += 1
    if not candidates:
        return None, []

    def score(start: int) -> int:
        entries = _read_seg_run(data, start)
        has_paslib1 = any((e[2] & 0x1FFF) == 17 for e in entries)
        return (1000 if has_paslib1 else 0) + len(entries)

    best = max(candidates, key=score)
    return best, _read_seg_run(data, best)


def _seg_lib_entry_ok(data: bytes, off: int) -> bool:
    """True if data[off:off+28] looks like one segment-directory record."""
    if off + 28 > len(data):
        return False
    if not is_plausible_seg_name(data[off : off + 8]):
        return False
    num = struct.unpack(">H", data[off + 8 : off + 10])[0]
    return (num & 0x1FFF) <= 255


def find_seg_lib(data: bytes) -> tuple[int | None, int | None, list[tuple[int, str, int, int, bytes, bytes, bytes]]]:
    """
    Locate the segment-library directory (9c 00 marker + 28-byte records).

    Returns (marker_offset, dir_offset, entries) or (None, None, []).
    """
    n = len(data)
    best = None
    idx = 0
    while True:
        m = data.find(SEG_LIB_MARKER, idx)
        if m < 0:
            break
        idx = m + 2
        start = m + 6
        if start + 28 * 2 > n:
            continue
        entries = []
        off = start
        while _seg_lib_entry_ok(data, off):
            name = fixed_len_bytes_to_string(data[off : off + 8])
            num_raw = struct.unpack(">H", data[off + 8 : off + 10])[0]
            num = num_raw & 0x1FFF
            desc = data[off + 10 : off + 18]
            extra = data[off + 18 : off + 28]
            entries.append((off, name, num_raw, num, desc, extra, data[off : off + 18]))
            off += 28
        if len(entries) >= 2 and (best is None or len(entries) > len(best[2])):
            best = (m, start, entries)
    if best is None:
        return None, None, []
    return best


def find_lib_file_id_table(data: bytes) -> dict[int, str]:
    """
    Locate the 9e 00 lib_file_id -> library-file-name table.

    Layout:
      2   9e 00 marker
      2   value (unused)
      2   entry count
      count * 6 bytes:
             2   lib_file_id
             4   offset of a Pascal string (1-byte length + chars)

    Returns {lib_file_id: file-name} for the best candidate, or {}.
    """
    best = {}
    idx = 0
    while True:
        m = data.find(b"\x9e\x00", idx)
        if m < 0:
            break
        idx = m + 2
        if m + 6 > len(data):
            continue
        count = struct.unpack(">H", data[m + 4 : m + 6])[0]
        if count == 0 or count > 64:
            continue
        base = m + 6
        table = {}
        ok = True
        for i in range(count):
            off = base + i * 6
            if off + 6 > len(data):
                ok = False
                break
            lib_file_id = struct.unpack(">H", data[off : off + 2])[0]
            ptr = struct.unpack(">I", data[off + 2 : off + 6])[0]
            if ptr + 1 > len(data):
                ok = False
                break
            ln = data[ptr]
            if ptr + 1 + ln > len(data) or ln == 0:
                ok = False
                break
            # Decode the length-prefixed Pascal string with the shared
            # pascal_to_string() helper (stops at the first NUL, decodes
            # mac-roman, which is identical to ascii for these names) and
            # strip a LisaFiling volume prefix (e.g. '-#BOOT-'), which is
            # not part of the file's name on the volume.
            name = strip_volume_prefix(pascal_to_string(data, start=ptr))
            if not name:
                ok = False
                break
            table[lib_file_id] = name
        if ok and len(table) > len(best):
            best = table
    return best


def scan_units_in_obj(file_as_bytes: bytes, file_name: str = "<obj>") -> list[tuple[int, str, int, int]]:
    """Parse and print the unit table of a Lisa Pascal object file.

    file_as_bytes is the file's complete contents (read from a disk image
    or from a host file); file_name is only used in the printed messages.

    Returns a list of (offset, name, unit_number, unit_type) tuples, or
    an empty list if no valid unit table was found.
    """
    data = file_as_bytes

    # ---- unit table -------------------------------------------------
    unit_entries = []
    marker, count = find_unit_table(data)
    if marker is None or count is None:
        print(f"No valid unit table found (9b 00 marker) in file {file_name}.")
    else:
        table_start = marker + 8
        for i in range(count):
            offset = table_start + i * 12
            raw_name = data[offset : offset + 8]
            unit_number = struct.unpack(">H", data[offset + 8 : offset + 10])[0]
            unit_type = struct.unpack(">H", data[offset + 10 : offset + 12])[0]
            unit_entries.append((offset, fixed_len_bytes_to_string(raw_name), unit_number, unit_type))

        print(f"\nUnit table (marker '{OBJ_UNIT_TABLE_MARKER.hex(' ')}' found at 0x{marker:06X}, {len(unit_entries)} entries):")
        print("----------------------------------------------")
        for offset, name, unit_number, unit_type in unit_entries:
            print(
                f"offset 0x{offset:06X}: "
                f"{name:<12} "
                f"unit_number={unit_number:3d} "
                f"unit_type={unit_type} ({unit_type_name(unit_type)})"
            )
        print(
            "Note: each entry above is 12 bytes long: "
            "8 bytes for name, 2 bytes for unit number and 2 bytes for unit_type "
            "(0=Regular, 1=Intrinsic, 2=Shared, everything else=Unknown)."
        )

    return unit_entries


def scan_segments_in_obj(file_as_bytes: bytes, file_name: str = "<obj>") -> list[tuple[int, str, int, bytes, bytes]]:
    """Parse and print the segment table of a Lisa Pascal object file.

    file_as_bytes is the file's complete contents (read from a disk image
    or from a host file); file_name is only used in the printed messages.

    Returns a list of (offset, name, segment_number, descriptor, raw)
    tuples, or an empty list if no segment table was found.
    """
    data = file_as_bytes

    # ---- segment table ---------------------------------------------
    seg_entries : list[tuple[int, str, int, bytes, bytes]] = []
    seg_start, segs = find_segment_table(data)
    if seg_start is None:
        print(f"\nNo segment table found in file {file_name}. The file may not be a Lisa executable file.")
    else:
        seg_entries = segs
        print(
            f"\nSegment table (at 0x{seg_start:06X}, {len(seg_entries)} entries) "
            f"-- code segments loaded from the shared library:"
        )
        print("--------------------------------------------------------------")
        for offset, name, segment_num, descriptor, _raw in seg_entries:
            version1 = descriptor[1:4]
            version2 = descriptor[5:]
            print(
                f"offset 0x{offset:06X}: "
                f"{name:<12}   "
                f"segment={segment_num:04X}   "
                f"Version1={version1.hex().upper()}   Version2={version2.hex().upper()}"
            )
        print(
            "Note: each entry above is 18 bytes long: "
            "8 bytes for name, 2 bytes for segment number, 1 byte for ???, 3 bytes for Version1, 1 byte for ???, 3 bytes for Version2."
        )

    return seg_entries


def find_lib_directory(data: bytes) -> int:
    """
    Locate the start of the INTRINSIC.LIB unit directory.

    The directory is a run of 16-byte records
    [8-byte name][2-byte unit][6 bytes].  Find the first offset where
    two consecutive records both look plausible.
    """

    def plausible(off: int) -> bool:
        if off + 16 > len(data):
            return False
        if not is_plausible_name(data[off : off + 8]):
            return False
        return struct.unpack(">H", data[off + 8 : off + 10])[0] <= 255

    for off in range(0, min(len(data), 256)):
        if plausible(off) and plausible(off + 16):
            return off
    return -1


def scan_units_in_lib(file_as_bytes: bytes, file_name: str = "<lib>") -> list[tuple[int, str, int, int]]:
    """Parse and print the unit directory of an INTRINSIC.LIB.

    file_as_bytes is the file's complete contents (read from a disk image
    or from a host file); file_name is only used in the printed messages.
    Each record is annotated with the library file name looked up in the
    lib_file_id -> library file-name table of the same file.

    Returns a list of (offset, name, unit_number, lib_file_id) tuples, or
    an empty list if no unit directory was found.
    """
    data = file_as_bytes

    # Look up the lib_file_id -> library file-name table, so each record
    # can be annotated with its library file.
    lib_file_id_table = find_lib_file_id_table(data)

    # ---- unit directory --------------------------------------------
    unit_entries : list[tuple[int, str, int, int]] = []
    start = find_lib_directory(data)
    if start < 0:
        print("Unit directory not found.")
    else:
        offset = start
        while offset + 16 <= len(data):
            raw_name = data[offset : offset + 8]
            unit_number = struct.unpack(">H", data[offset + 8 : offset + 10])[0]
            if not is_plausible_name(raw_name) or unit_number > 255:
                break
            lib_file_id = data[offset + 10]
            unit_entries.append((offset, fixed_len_bytes_to_string(raw_name), unit_number, lib_file_id))
            offset += 16

        print(
            f"\nUnit directory (starts at 0x{start:06X}, {len(unit_entries)} records):"
        )
        print("--------------------------------------------------------------")
        for offset, unit_name, unit_number, lib_file_id in unit_entries:
            libname = lib_file_id_table.get(lib_file_id, "Unknown library file")
            print(
                f"offset 0x{offset:06X}: {unit_name:<12} unit_number={unit_number:<4} "
                f"lib_file_id={lib_file_id} (file {libname})"
            )
        print(
            "Note: each record above is 16 bytes long: 8 bytes for name, "
            "2 bytes for unit number, 1 byte for lib_file_id, 1 byte for flag "
            "and 4 bytes for size/extra."
        )

    return unit_entries


def scan_segments_in_lib(file_as_bytes: bytes, file_name: str = "<lib>") -> list[tuple[int, str, int, int, bytes, bytes, int, bytes]]:
    """Parse and print the segment directory of an INTRINSIC.LIB.

    file_as_bytes is the file's complete contents (read from a disk image
    or from a host file); file_name is only used in the printed messages.
    Each record is annotated with the library file name looked up in the
    lib_file_id -> library file-name table of the same file.

    Returns a list of (offset, name, segment_number_raw, segment_number,
    descriptor, extra, lib_file_id, raw) tuples, or an empty list if no
    segment directory was found.
    """
    data = file_as_bytes

    # Look up the lib_file_id -> library file-name table, so each record
    # can be annotated with its library file.
    lib_file_id_table = find_lib_file_id_table(data)

    # ---- segment directory -----------------------------------------
    seg_entries = []
    seg_marker, seg_dir, segs = find_seg_lib(data)
    if seg_marker is None:
        print("\nSegment directory not found (9c 00 marker).")
    else:
        for offset, unit_name, segment_num_raw, segment_num, descriptor, extra, raw18 in segs:
            lib_file_id = extra[1] if len(extra) >= 2 else 0
            seg_entries.append((offset, unit_name, segment_num_raw, segment_num, descriptor, extra, lib_file_id, raw18))
        print(
            f"\nSegment directory (marker '{SEG_LIB_MARKER.hex(' ')}' found at 0x{seg_marker:06X}, "
            f"dir at 0x{seg_dir:06X}, {len(seg_entries)} records):"
        )
        print("--------------------------------------------------------------")
        for offset, unit_name, segment_num_raw, segment_num, descriptor, extra, lib_file_id, raw18 in seg_entries:
            version1 = descriptor[1:4]
            version2 = descriptor[5:]
            libname = lib_file_id_table.get(lib_file_id, "Unknown library file")
            print(
                f"offset 0x{offset:06X}: "
                f"{unit_name:<12} "
                f"segment={segment_num:04X}   "
                f"lib_file_id={lib_file_id:<3} "
                f"(file {libname:<14})   "
                f"   Version1={version1.hex().upper()}   Version2={version2.hex().upper()}"
            )
        print(
            "Note: each record above is 28 bytes long: 8 bytes for name, "
            "2 bytes for segment number, 1 byte for ???, 3 bytes for Version1, 1 byte for ???, 3 bytes for Version2, "
            "10 bytes for extra - the lib_file_id byte is the 2nd byte of "
            "that extra field (record offset 19)."
        )

    return seg_entries


def scan_lib_file_id_table_in_lib(file_as_bytes: bytes, file_name: str = "<lib>") -> dict[int, str]:
    """Parse and print the lib_file_id -> library file-name table
    (9e 00 marker) of an INTRINSIC.LIB.

    file_as_bytes is the file's complete contents (read from a disk image
    or from a host file); file_name is only used in the printed messages.

    Returns the {lib_file_id: library file name} dictionary, or {} if no
    valid table was found.
    """
    data = file_as_bytes
    lib_file_id_table = find_lib_file_id_table(data)

    # ---- lib_file_id -> library file name table --------------------
    if lib_file_id_table:
        print(f"\nlib_file_id -> library file-name table ({len(lib_file_id_table)} entries):")
        print("--------------------------------------------------------------")
        for lib_file_id in sorted(lib_file_id_table):
            print(f"  lib_file_id {lib_file_id:<3} -> {lib_file_id_table[lib_file_id]}")
        print(
            "Note: each entry above is 6 bytes long: 2 bytes for the "
            "lib_file_id and 4 bytes for an offset pointing at a Pascal "
            "string (1-byte length + characters) holding the library "
            "file name."
        )

    return lib_file_id_table


class LisaExecutableFileAnalyzer(FileSystemWithPerFileCommands):
    """A Lisa disk image (loaded by the constructor, see
    FileSystemWithPerFileCommands / InMemoryFileSystem) plus the analysis
    of a Lisa executable (object) file stored on the volume: analyze()
    reads the .OBJ file and the hard-coded INTRINSIC.LIB from the volume,
    parses both, and prints a comparison of the units and code segments
    the object file references with the entries of the intrinsic library.
    """

    def read_file_from_disk_image(self, lisa_name: str) -> bytes:
        """Read the file named lisa_name from the disk image and return its
        complete contents as bytes.

        Exits the program with a message if the file cannot be read.

        To work with ordinary files on the computer's file system instead of
        a disk image, replace the calls to this method in analyze() with
        `open(host_file_name, "rb").read()` - the scan functions only need
        the bytes, not a file name.
        """
        file_as_bytes = self.read_file_as_bytes(lisa_name)
        if file_as_bytes is None:
            print(f"ERROR: could not read '{lisa_name}' from the disk image; aborting.")
            sys.exit(1)
        return file_as_bytes

    def file_exists_on_volume(self, lisa_name: str) -> bool:
        """True if a regular file named lisa_name is on this volume
        (case-insensitive catalog lookup only; the file's data is not read).
        """
        located = self._locate_named_file(lisa_name)
        return located.status == "file"

    def analyze(self, obj_file_name: str) -> None:
        """Read the .OBJ file named obj_file_name and the hard-coded
        INTRINSIC.LIB (INTRINSIC_LIB_NAME) from this disk image, parse
        both, and print a comparison of the units and code segments the
        object file references with the entries of the intrinsic library.
        """
        obj_file_as_bytes = self.read_file_from_disk_image(obj_file_name)
        lib_file_as_bytes = self.read_file_from_disk_image(INTRINSIC_LIB_NAME)

        # The name lookup is case-insensitive, so obj_file_name may differ
        # in case from the name stored on the volume; located.name is the
        # file's actual name (None if the file is not on the volume, e.g.
        # when the reads above were replaced by host-file reads).
        located = self._locate_named_file(obj_file_name)
        actual_obj_name = located.name if located.name is not None else obj_file_name

        print(f"\n\n=== EXECUTABLE FILE: {obj_file_name} (of size: {len(obj_file_as_bytes)} bytes) ===")
        obj_units : list[tuple[int, str, int, int]] = scan_units_in_obj(obj_file_as_bytes, obj_file_name)
        obj_segments : list[tuple[int, str, int, str, bytes]] = scan_segments_in_obj(obj_file_as_bytes, obj_file_name)

        print(f"\n\n\n=== INTRINSIC LIBRARY: {INTRINSIC_LIB_NAME} (of size: {len(lib_file_as_bytes)} bytes) ===")
        all_intrinsic_units : list[tuple[int, str, int, int]] = scan_units_in_lib(lib_file_as_bytes, INTRINSIC_LIB_NAME)
        all_intrinsic_segments : list[tuple[int, str, int, str, bytes]] = scan_segments_in_lib(lib_file_as_bytes, INTRINSIC_LIB_NAME)
        intrinsic_file_id_table : list[tuple[int, int]] = scan_lib_file_id_table_in_lib(lib_file_as_bytes, INTRINSIC_LIB_NAME)

        # The same name can occur more than once in a table, so keep the
        # first entry we see: rebuild obj_units without later duplicates,
        # warning if a duplicate carries a different unit number.
        deduped = []
        for entry in obj_units:
            unit_name, unit_number = entry[1], entry[2]
            same_name = [e for e in deduped if e[1] == unit_name]
            if same_name:
                if any(e[2] != unit_number for e in same_name):
                    print(f"\nWARNING: {unit_name} appears in OBJ with two unit numbers.")
            else:
                deduped.append(entry)
        obj_units = deduped

        # The same name can occur more than once in a table, so keep the
        # first entry we see: rebuild lib_units without later duplicates,
        # warning if a duplicate carries a different unit number.
        deduped = []
        for entry in all_intrinsic_units:
            unit_name, unit_number = entry[1], entry[2]
            same_name = [e for e in deduped if e[1] == unit_name]
            if same_name:
                if any(e[2] != unit_number for e in same_name):
                    print(f"\nWARNING: {unit_name} appears in LIB with two unit numbers.")
            else:
                deduped.append(entry)
        all_intrinsic_units = deduped

        # The same name can occur more than once in a table, so keep the
        # first entry we see: rebuild obj_segments without later duplicates,
        # warning if a duplicate carries a different segment number.
        deduped = []
        for entry in obj_segments:
            seg_name, seg_num = entry[1], entry[2]
            same_name = [e for e in deduped if e[1] == seg_name]
            if same_name:
                if any(e[2] != seg_num for e in same_name):
                    print(f"\nWARNING: segment {seg_name} appears in OBJ with two numbers.")
            else:
                deduped.append(entry)
        obj_segments = deduped

        # The same name can occur more than once in a table, so keep the
        # first entry we see: rebuild lib_segments without later duplicates,
        # warning if a duplicate carries a different segment number.
        deduped = []
        for entry in all_intrinsic_segments:
            seg_name, seg_num = entry[1], entry[3]
            same_name = [e for e in deduped if e[1] == seg_name]
            if same_name:
                if any(e[3] != seg_num for e in same_name):
                    print(f"\nWARNING: segment {seg_name} appears in LIB with two numbers.")
            else:
                deduped.append(entry)
        all_intrinsic_segments = deduped

        # Every problem the comparison detects is collected in this list;
        # the summary line printed at the end of analyze() is based on it.
        problems: list[str] = []

        def lib_file_id_note(lib_file_id: int) -> str:
            """Return a short note about the library file selected by the
            lib_file_id, using the lib_file_id -> library file-name table
            of INTRINSIC.LIB and a read-only check that the library file
            is on the volume.  Possible results:
              'lib_file_id=NN (no library file name in INTRINSIC.LIB)'
              'lib_file_id=NN (file <name padded to 14 chars>, OK: file exists)'
              'lib_file_id=NN (file <name>, !!!!! FILE NOT FOUND ON VOLUME !!!!!)'
            A missing (or unresolvable) library file is also recorded in
            the problems list.
            """
            libname = intrinsic_file_id_table.get(lib_file_id)
            if libname is None:
                problem = f"lib_file_id {lib_file_id:02d} has no library file name in INTRINSIC.LIB"
                if problem not in problems:
                    problems.append(problem)
                return f"lib_file_id={lib_file_id:02d} (no library file name in INTRINSIC.LIB)"
            # The table may carry a leading '*' or tab marker; the file on
            # the volume can be named with or without it, so try both.
            candidates = [libname]
            stripped = libname.lstrip("*\t")
            if stripped and stripped != libname:
                candidates.append(stripped)
            if any(self.file_exists_on_volume(n) for n in candidates):
                return f"lib_file_id={lib_file_id:02d} (file {libname:<14}, OK: file exists)"
            problem = f"intrinsic library file '{libname}' (lib_file_id {lib_file_id:02d}) not found on volume"
            if problem not in problems:
                problems.append(problem)
            return f"lib_file_id={lib_file_id:02d} (file {libname}, !!!!! FILE NOT FOUND ON VOLUME !!!!!)"

        print("\n\n\n=== COMPARISON ===")

        if not obj_units:
            print(f"\nNo unit references were detected in file {actual_obj_name}. The file may not be a Lisa executable file.")
            problems.append(f"no unit references detected in file {actual_obj_name} (the file may not be a Lisa executable file)")
        else:
            print(f"\nUnits referenced by executable file '{actual_obj_name}':")
            print("-------------------------")
            for offset, unit_name_in_obj, unit_num_in_obj, unit_type in sorted(obj_units, key=lambda e: e[1]):
                matching = [e for e in all_intrinsic_units if e[1] == unit_name_in_obj]
                if matching:
                    unit_num_in_intrinsic_lib = matching[0][2]
                    intrinsic_lib_file_id = matching[0][3]
                    if unit_num_in_obj == unit_num_in_intrinsic_lib:
                        status = "OK(Unit numbers match)"
                    else:
                        status = "!!!!! UNIT NUMBERS MISMATCH !!!!!!"
                        problems.append(f"unit {unit_name_in_obj}: unit number mismatch (OBJ={unit_num_in_obj}, LIB={unit_num_in_intrinsic_lib})")
                    print(
                        f"{unit_name_in_obj:<12} Unit-in-OBJ={unit_num_in_obj:<4} Unit-in-LIB={unit_num_in_intrinsic_lib:<4} "
                        f"{status} {lib_file_id_note(intrinsic_lib_file_id)}"
                    )
                else:
                    print(f"{unit_name_in_obj:<12} Unit-in-OBJ={unit_num_in_obj:<4}   Unit-in-LIB=---- MISSING")
                    problems.append(f"unit {unit_name_in_obj} (unit number {unit_num_in_obj}) not found in INTRINSIC.LIB")

            print(f"\nSegments needed by executable file '{actual_obj_name}':")
            print("--------------------------------------------------------------")
            if not obj_segments:
                print("(none found)")
                problems.append(f"no segment table found in file {actual_obj_name} (the file may not be a Lisa executable file)")
            for offset, seg_name_in_obj, seg_num_in_obj, desc, obj_raw in sorted(obj_segments, key=lambda e: e[1]):
                matching = [e for e in all_intrinsic_segments if e[1] == seg_name_in_obj]
                if matching:
                    seg_num_in_lib = matching[0][3]
                    intrinsic_lib_file_id = matching[0][6]
                    lib_raw = matching[0][7]
                    # Compare the whole 18-byte segment entry, which includes the segment name, number, Version1, Version2, and other metadata.
                    if obj_raw == lib_raw:
                        status = "OK(18 bytes match)"
                    else:
                        status = "!!!!! 18-BYTE SEGMENT ENTRIES MISMATCH !!!!!"
                        problems.append(f"segment {seg_name_in_obj}: 18-byte segment entries differ between OBJ and LIB")
                    print(
                        f"{seg_name_in_obj:<12}   "
                        f"{status} {lib_file_id_note(intrinsic_lib_file_id)}"
                    )
                    if obj_raw != lib_raw:
                        print(f"{'':<12} OBJ 18 bytes: {obj_raw.hex(' ')}")
                        print(f"{'':<12} LIB 18 bytes: {lib_raw.hex(' ')}")
                else:
                    print(
                        f"{seg_name_in_obj:<12} Segment-in-OBJ={seg_num_in_obj:04X}   "
                        f"Segment-in-LIB=---- (not found in INTRINSIC.LIB!)  "
                    )
                    problems.append(f"segment {seg_name_in_obj} (segment number {seg_num_in_obj:04X}) not found in INTRINSIC.LIB")

        # ---- group units and segments by library (lib_file_id byte) ----
        # if lib_units or lib_segments:
        #     print("\n=== UNITS AND SEGMENTS GROUPED BY LIBRARY ===")
        #     print("Both the unit directory and the segment directory carry a")
        #     print("'lib_file_id' byte; it selects the shared library file")
        #     print("that holds the code.  A segment is the code of the unit(s)")
        #     print("in its library, so units and segments are linked by")
        #     print("lib_file_id rather than by a one-to-one name or number.")
        #     print()

        #     unit_lib_file_ids = {}
        #     for off, name, unit, lib_file_id in lib_units:
        #         unit_lib_file_ids.setdefault(lib_file_id, []).append((name, unit))
        #     seg_lib_file_ids = {}
        #     for off, name, num_raw, num, desc, extra, lib_file_id in lib_segments:
        #         seg_lib_file_ids.setdefault(lib_file_id, []).append((name, num))

        #     req_units = set(requested_units)
        #     req_segs = set(requested_segs)

        #     for lib_file_id in sorted(set(unit_lib_file_ids) | set(seg_lib_file_ids)):
        #         libname = lib_file_id_table.get(lib_file_id, "?")
        #         us = unit_lib_file_ids.get(lib_file_id, [])
        #         ss = seg_lib_file_ids.get(lib_file_id, [])
        #         ustr = "  ".join(
        #             f"{n}({u})" + ("*" if n in req_units else "") for n, u in us
        #         )
        #         sstr = "  ".join(
        #             f"{n}({s})" + ("*" if n in req_segs else "") for n, s in ss
        #         )
        #         print(f"lib_file_id {lib_file_id:<3} -> {libname}")
        #         print(f"   units   ({len(us):<2}): {ustr}")
        #         print(f"   segments({len(ss):<2}): {sstr}")
        #         print()
        #     print("(* = referenced by the OBJ file)")
        #     print()

        # Detect duplicate unit numbers in INTRINSIC.LIB.
        by_unit = {}
        for offset, unit_name, unit_number, intrinsic_lib_file_id in all_intrinsic_units:
            by_unit.setdefault(unit_number, []).append(unit_name)

        duplicates = {u: n for u, n in by_unit.items() if len(n) > 1}
        if duplicates:
            print("\nWARNING: duplicate unit numbers found in INTRINSIC.LIB:")
            print("----------------------------------------------")
            for unit_number, names in sorted(duplicates.items()):
                print(f"unit {unit_number}: " + ", ".join(names))

        # ---- summary ---------------------------------------------------
        if problems:
            print("\n!!!!! PROBLEMS FOUND !!!!! :")
            for problem in problems:
                print(f"  - {problem}")
        else:
            print(f"\nEverything matches and all intrinsic library files needed to execute file '{obj_file_name}' are present on the disk image.")

        print("\nDone.")


def main() -> None:
    if len(sys.argv) != 3:
        print(
            "Prints the shared (also known as 'intrinsic') libraries used by a Lisa executable file (present on the given disk image).\n"
            "Usage:\n"
            "    python3 AnalyzeLisaExecutableFile.py <disk image file name> <name of executable .OBJ file>"
        )
        sys.exit(1)

    disk_image_file_name = sys.argv[1]
    obj_file_name = sys.argv[2]

    try:
        analyzer = LisaExecutableFileAnalyzer(disk_image_file_name)
    except FileNotFoundError:
        print(f"File {disk_image_file_name} not found!")
        sys.exit(1)
    except IOError as e:
        print(f"IO Error: '{e}' while reading file {disk_image_file_name} !")
        sys.exit(1)
    except Exception as e:
        print(
            f"ERROR: {type(e).__name__}: {e} while reading file {disk_image_file_name}! Exiting."
        )
        sys.exit(1)

    analyzer.analyze(obj_file_name)


if __name__ == "__main__":
    main()
