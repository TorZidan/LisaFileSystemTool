#!/usr/bin/env python3
"""
List the intrinsic unit names and numbers stored in a Lisa Pascal
INTRINSIC.LIB file and compare them with the units *and* the code
segments referenced by a Workshop object file (.OBJ).

Both files are read from a Lisa disk image (DC42 or raw ProFile), whose
name is the first command-line argument; the second argument is the name
of the .OBJ file as stored on the volume. The name of the INTRINSIC.LIB
file is hard-coded (INTRINSIC_LIB_NAME). The reading is done with the
code of LisaFileSystemTool.py / LisaFileSystemToolPerFile.py
(FileSystemWithPerFileCommands and its read_file_as_bytes() method).

The parsing operates on the files' contents as plain `bytes` objects
(file_as_bytes), not on host file names, so switching back to ordinary
files on the computer's file system is a one-line change: replace the
read_file_from_disk_image() calls in analyze() with
`open(host_file_name, "rb").read()`.

Both file types are big-endian.

Background
----------
A Workshop / Lisa Pascal program does not carry the code of the intrinsic
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
             1   type
             1   flag
             4   size / extra
  0x5A2  9c 00             segment-library marker
  0x5A8  segment directory, 28-byte records:
             8   segment name, padded with spaces
             2   segment number
             8   descriptor
             10  extra (byte 1 = type)
  ...    9e 00             type -> library file-name table:
             2   value (unused)
             2   entry count
             6 * count:
                   2   type number
                   4   offset of a Pascal-string file name

Unit table in an .OBJ file
  0x1C   9b 00             unit-table marker (searched for anywhere,
                           because linked files embed it deeper)
  0x1E   16-bit value      (not used)
  0x20   16-bit entry count
  0x22   16-bit highest unit number
  0x24   entries, 12 bytes each:
             8   unit name, padded with spaces
             2   unit number (some compilers set a 0x2000 flag bit)
             2   flag

Segment table in an .OBJ file
  A contiguous run of 18-byte entries:
             8   segment name, padded with spaces (mixed case)
             2   segment number (some compilers set a 0x2000 flag bit)
             8   descriptor
  The run is located by scanning for stretches of valid entries; a stretch
  that contains PASLIB1 (segment 17, present in virtually every program)
  is preferred, then the longest.
"""

import struct
import sys
from typing import Any

from LisaFileSystemTool import pascal_to_string
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


def clean_name(raw: bytes) -> str:
    """Convert a fixed-length, space-padded name field to text."""
    return raw.split(b"\x00", 1)[0].rstrip(b" ").decode("ascii", "replace")


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


def _read_seg_run(data: bytes, start: int) -> list[tuple[int, str, int, bytes]]:
    """Read consecutive segment-table entries starting at start."""
    entries = []
    off = start
    while _seg_entry_ok(data, off):
        name = clean_name(data[off : off + 8])
        num = struct.unpack(">H", data[off + 8 : off + 10])[0]
        desc = data[off + 10 : off + 18]
        entries.append((off, name, num, desc))
        off += 18
    return entries


def find_segment_table(data: bytes) -> tuple[int | None, list[tuple[int, str, int, bytes]]]:
    """
    Locate the program's segment table (a run of 18-byte entries).

    Returns (start_offset, entries) or (None, []).  When several
    candidate runs exist, the one containing PASLIB1 (segment 17) is
    preferred, then the longest.
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


def find_seg_lib(data: bytes) -> tuple[int | None, int | None, list[tuple[int, str, int, int, bytes, bytes]]]:
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
            name = clean_name(data[off : off + 8])
            num_raw = struct.unpack(">H", data[off + 8 : off + 10])[0]
            num = num_raw & 0x1FFF
            desc = data[off + 10 : off + 18]
            extra = data[off + 18 : off + 28]
            entries.append((off, name, num_raw, num, desc, extra))
            off += 28
        if len(entries) >= 2 and (best is None or len(entries) > len(best[2])):
            best = (m, start, entries)
    if best is None:
        return None, None, []
    return best


def find_type_table(data: bytes) -> dict[int, str]:
    """
    Locate the 9e 00 type -> library-file-name table.

    Layout:
      2   9e 00 marker
      2   value (unused)
      2   entry count
      count * 6 bytes:
             2   type number
             4   offset of a Pascal string (1-byte length + chars)

    Returns {type: file-name} for the best candidate, or {}.
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
            typ = struct.unpack(">H", data[off : off + 2])[0]
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
            table[typ] = name
        if ok and len(table) > len(best):
            best = table
    return best


def scan_obj(file_as_bytes: bytes, file_name: str = "<obj>") -> dict[str, Any]:
    """Parse the unit table and segment table of a Lisa Pascal object file.

    file_as_bytes is the file's complete contents (read from a disk image
    or from a host file); file_name is only used in the printed header.
    """
    data = file_as_bytes

    print(f"\n\n=== Executable .OBJ FILE: {file_name} ===")
    print(f"Size: {len(data)} bytes")

    # ---- unit table -------------------------------------------------
    unit_entries = []
    marker, count = find_unit_table(data)
    if marker is None or count is None:
        print("No valid unit table found (9b 00 marker).")
    else:
        table_start = marker + 8
        for i in range(count):
            off = table_start + i * 12
            raw_name = data[off : off + 8]
            unit = struct.unpack(">H", data[off + 8 : off + 10])[0]
            flag = struct.unpack(">H", data[off + 10 : off + 12])[0]
            # The field holds the unit number the runtime reads. Some
            # compilers set a 0x2000 flag bit in it (e.g. 0x2001 for unit
            # 1); the runtime does not mask the bit, so it looks up 8193
            # instead of 1 and fails with error 143.
            unit_entries.append((off, clean_name(raw_name), unit, flag))

        print(f"\nUnit table (marker '{OBJ_UNIT_TABLE_MARKER.hex(' ')}' found at 0x{marker:06X}, {len(unit_entries)} entries):")
        print("----------------------------------------------")
        for off, name, unit, flag in unit_entries:
            print(
                f"offset 0x{off:06X}: "
                f"{name:<12} "
                f"unit={unit:3d} "
                f"flag={flag}"
            )
        print(
            "Note: each entry above is 12 bytes long: "
            "8 bytes for name, 2 bytes for unit number and 2 bytes for flag."
        )

    # ---- segment table ---------------------------------------------
    seg_entries : list[tuple[int, str, int, bytes]] = []
    seg_start, segs = find_segment_table(data)
    if seg_start is None:
        print("\nNo segment table found.")
    else:
        seg_entries = segs
        print(
            f"\nSegment table (at 0x{seg_start:06X}, {len(seg_entries)} entries) "
            f"-- code segments loaded from the shared library:"
        )
        print("--------------------------------------------------------------")
        for off, name, num, desc in seg_entries:
            print(
                f"offset 0x{off:06X}: "
                f"{name:<12} "
                f"segment={num} "
                f"desc={desc.hex(' ')}"
            )
        print(
            "Note: each entry above is 18 bytes long: "
            "8 bytes for name, 2 bytes for segment number and 8 bytes for descriptor."
        )

    return {"units": unit_entries, "segments": seg_entries}


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


def scan_intrinsic_lib(file_as_bytes: bytes, file_name: str = "<lib>") -> dict[str, Any]:
    """Parse the unit directory and segment directory of an INTRINSIC.LIB.

    file_as_bytes is the file's complete contents (read from a disk image
    or from a host file); file_name is only used in the printed header.
    """
    data = file_as_bytes

    print(f"\n\n\n=== INTRINSIC LIBRARY: {file_name} ===")
    print(f"Size: {len(data)} bytes")

    # Parse the type -> library file-name table first, so the unit and
    # segment dumps below can annotate each record with its library file.
    type_table = find_type_table(data)

    # ---- unit directory --------------------------------------------
    unit_entries = []
    start = find_lib_directory(data)
    if start < 0:
        print("Unit directory not found.")
    else:
        off = start
        while off + 16 <= len(data):
            raw_name = data[off : off + 8]
            unit = struct.unpack(">H", data[off + 8 : off + 10])[0]
            if not is_plausible_name(raw_name) or unit > 255:
                break
            typ = data[off + 10]
            unit_entries.append((off, clean_name(raw_name), unit, typ))
            off += 16

        print(
            f"\nUnit directory (starts at 0x{start:06X}, {len(unit_entries)} records):"
        )
        print("--------------------------------------------------------------")
        for off, name, unit, typ in unit_entries:
            libname = type_table.get(typ, "Unknown library file")
            print(
                f"offset 0x{off:06X}: {name:<12} unit={unit:<4} "
                f"type={typ} ({libname})"
            )
        print(
            "Note: each record above is 16 bytes long: 8 bytes for name, "
            "2 bytes for unit number, 1 byte for type, 1 byte for flag "
            "and 4 bytes for size/extra."
        )

    # ---- segment directory -----------------------------------------
    seg_entries = []
    seg_marker, seg_dir, segs = find_seg_lib(data)
    if seg_marker is None:
        print("\nSegment directory not found (9c 00 marker).")
    else:
        for off, name, num_raw, num, desc, extra in segs:
            typ = extra[1] if len(extra) >= 2 else 0
            seg_entries.append((off, name, num_raw, num, desc, extra, typ))
        print(
            f"\nSegment directory (marker '{SEG_LIB_MARKER.hex(' ')}' found at 0x{seg_marker:06X}, "
            f"dir at 0x{seg_dir:06X}, {len(seg_entries)} records):"
        )
        print("--------------------------------------------------------------")
        for off, name, num_raw, num, desc, extra, typ in seg_entries:
            libname = type_table.get(typ, "Unknown library file")
            print(
                f"offset 0x{off:06X}: "
                f"{name:<12} "
                f"segment={num:<4} "
                f"type={typ:<3} "
                f"({libname:<14}) "
                f"desc={desc.hex(' ')}"
            )
        print(
            "Note: each record above is 28 bytes long: 8 bytes for name, "
            "2 bytes for segment number, 8 bytes for descriptor "
            "and 10 bytes for extra - the type byte is the 2nd byte of "
            "that extra field (record offset 19)."
        )

    # ---- type -> library file name table ---------------------------
    if type_table:
        print(f"\nType -> library file table ({len(type_table)} entries):")
        print("--------------------------------------------------------------")
        for typ in sorted(type_table):
            print(f"  type {typ:<3} -> {type_table[typ]}")
        print(
            "Note: each entry above is 6 bytes long: 2 bytes for the type "
            "number and 4 bytes for an offset pointing at a Pascal string "
            "(1-byte length + characters) holding the library file name."
        )

    return {"units": unit_entries, "segments": seg_entries, "type_table": type_table}


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

        obj = scan_obj(obj_file_as_bytes, obj_file_name)
        lib = scan_intrinsic_lib(lib_file_as_bytes, INTRINSIC_LIB_NAME)

        # Build dictionaries.  The same name can occur more than once in a
        # table, so keep the first number we see.
        requested_units = {}
        for off, name, unit, flag in obj["units"]:
            if name in requested_units and requested_units[name] != unit:
                print(f"\nWARNING: {name} appears in OBJ with two unit numbers.")
            requested_units.setdefault(name, unit)

        available_units = {}
        available_unit_types = {}
        for off, name, unit, typ in lib["units"]:
            if name in available_units and available_units[name] != unit:
                print(f"\nWARNING: {name} appears in LIB with two unit numbers.")
            available_units.setdefault(name, unit)
            available_unit_types.setdefault(name, typ)

        requested_segs = {}
        for off, name, num, desc in obj["segments"]:
            if name in requested_segs and requested_segs[name] != num:
                print(f"\nWARNING: segment {name} appears in OBJ with two numbers.")
            requested_segs.setdefault(name, num)

        available_segs = {}
        available_seg_types = {}
        for off, name, num_raw, num, desc, extra, typ in lib["segments"]:
            if name in available_segs and available_segs[name] != num:
                print(f"\nWARNING: segment {name} appears in LIB with two numbers.")
            available_segs.setdefault(name, num)
            available_seg_types.setdefault(name, typ)

        type_table = lib.get("type_table", {})

        def type_note(typ: int) -> str:
            """'type=N (LIBFILE, file exists)' for the type byte typ, using
            the type -> library file-name table of INTRINSIC.LIB and a
            read-only check that the library file is on the volume."""
            libname = type_table.get(typ)
            if libname is None:
                return f"type={typ:02d} (no library file name in INTRINSIC.LIB)"
            # The table may carry a leading '*' or tab marker; the file on
            # the volume can be named with or without it, so try both.
            candidates = [libname]
            stripped = libname.lstrip("*\t")
            if stripped and stripped != libname:
                candidates.append(stripped)
            if any(self.file_exists_on_volume(n) for n in candidates):
                return f"type={typ:02d} (in {libname:<14}, OK: file exists)"
            return f"type={typ:02d} ({libname}, !!!!! FILE NOT FOUND ON VOLUME !!!!!)"

        print("\n\n\n=== COMPARISON ===")

        if not requested_units:
            print("\nNo unit references were detected in the OBJ file.")
            print("The file may not be a Lisa Pascal object file.")
        else:
            print(f"\nUnits referenced by OBJ file '{actual_obj_name}':")
            print("-------------------------")
            for name, obj_unit in sorted(requested_units.items()):
                if name in available_units:
                    lib_unit = available_units[name]
                    lib_type = available_unit_types[name]
                    status = "OK(Unit numbers match)" if obj_unit == lib_unit else "!!!!! UNIT NUMBERS MISMATCH !!!!!!"
                    print(
                        f"{name:<12} Unit-in-OBJ={obj_unit:<4} Unit-in-LIB={lib_unit:<4} "
                        f"{status} {type_note(lib_type)}"
                    )
                else:
                    print(f"{name:<12} Unit-in-OBJ={obj_unit:<4} Unit-in-LIB=---- MISSING")
                    print(f"{name:<12} Unit-in-OBJ={obj_unit:<4} Unit-in-LIB=---- MISSING")
                    print(f"{name:<12} Unit-in-OBJ={obj_unit:<4} UnitInLILIB=---- MISSING")

            print(f"\nSegments needed by OBJ file '{actual_obj_name}' (code loaded from the shared library):")
            print("--------------------------------------------------------------")
            if not requested_segs:
                print("(none found)")
            for name, obj_seg in sorted(requested_segs.items()):
                seg_bit = bool(obj_seg & 0x2000)
                if name in available_segs:
                    lib_seg = available_segs[name]
                    lib_type = available_seg_types[name]
                    status = "OK(Segment numbers match)" if obj_seg == lib_seg else "!!!!! SEGMENT NUMBERS MISMATCH !!!!!"
                    print(
                        f"{name:<12} Segment-in-OBJ={obj_seg:<4} Segment-in-LIB={lib_seg:<4} "
                        f"{status} {type_note(lib_type)}"
                    )
                else:
                    print(
                        f"{name:<12} Segment-in-OBJ={obj_seg:<4} "
                        f"Segment-in-LIB=---- (not in INTRINSIC.LIB; user segment or other library)"
                    )   

        # ---- group units and segments by library (type byte) -----------
        # type_table = lib.get("type_table", {})
        # if lib["units"] or lib["segments"]:
        #     print("\n=== UNITS AND SEGMENTS GROUPED BY LIBRARY ===")
        #     print("Both the unit directory and the segment directory carry a")
        #     print("'type' byte; it selects the shared library file that holds")
        #     print("the code.  A segment is the code of the unit(s) in its")
        #     print("library, so units and segments are linked by type rather")
        #     print("than by a one-to-one name or number.")
        #     print()

        #     unit_types = {}
        #     for off, name, unit, typ in lib["units"]:
        #         unit_types.setdefault(typ, []).append((name, unit))
        #     seg_types = {}
        #     for off, name, num_raw, num, desc, extra, typ in lib["segments"]:
        #         seg_types.setdefault(typ, []).append((name, num))

        #     req_units = set(requested_units)
        #     req_segs = set(requested_segs)

        #     for typ in sorted(set(unit_types) | set(seg_types)):
        #         libname = type_table.get(typ, "?")
        #         us = unit_types.get(typ, [])
        #         ss = seg_types.get(typ, [])
        #         ustr = "  ".join(
        #             f"{n}({u})" + ("*" if n in req_units else "") for n, u in us
        #         )
        #         sstr = "  ".join(
        #             f"{n}({s})" + ("*" if n in req_segs else "") for n, s in ss
        #         )
        #         print(f"type {typ:<3} -> {libname}")
        #         print(f"   units   ({len(us):<2}): {ustr}")
        #         print(f"   segments({len(ss):<2}): {sstr}")
        #         print()
        #     print("(* = referenced by the OBJ file)")
        #     print()

        # Detect duplicate unit numbers in the library.
        by_unit = {}
        for name, unit in available_units.items():
            by_unit.setdefault(unit, []).append(name)

        duplicates = {u: n for u, n in by_unit.items() if len(n) > 1}
        if duplicates:
            print("\nWARNING: duplicate unit numbers in library:")
            print("----------------------------------------------")
            for unit, names in sorted(duplicates.items()):
                print(f"unit {unit}: " + ", ".join(names))

        print("\nDone.")


def main() -> None:
    if len(sys.argv) != 3:
        print(
            "Prints the shared (aka intrinsic) libraries used by a Lisa executable file (present on the given disk image).\n"
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
