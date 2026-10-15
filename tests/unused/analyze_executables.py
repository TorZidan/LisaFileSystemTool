#!/usr/bin/env python3
"""
Analyze every file on a Lisa disk image and classify each one as one of:

    TEXT               a Lisa text file (name ends in ".TEXT")
    EXECUTABLE         a Lisa Pascal program object (references units)
    LIBRARY            a Lisa Pascal library object (provides units/segments)
    LIBRARY DIRECTORY  the intrinsic library index (INTRINSIC.LIB) -- a
                       directory of libraries, not a library itself
    (UNKNOWN)  everything else (OS, boot, fonts, ...)

The TEXT category is decided by the file name (a ".TEXT" extension); the
other categories are decided from the file's bytes.

Run:
    python3 tests/analyze_executables.py [disk image]

(with no argument it analyzes the default image
LisaSourceCompilation/LOS_Compilation_Base.image).

The disk image is only read, never written.

How a file is classified
------------------------
The volume is enumerated for both catalog styles: flat catalogs
(fs_version 14/15) are walked via the slist, and B-tree catalogs
(fs_version 16/17) are walked the same way the "list" command's
dump_catalog() walks them (descend to the leftmost leaf, then follow the
leaf chain). The byte-level parsing reuses the code of
AnalyzeLisaExecutableFile.py (find_unit_table(), find_segment_table(),
find_seg_lib() and the name-validation helpers) on top of
FileSystemWithPerFileCommands; each file's data is read with
flat_catalog_read_file_data(), which works for both catalog styles because
only the catalog differs, not the on-disk file-data layout (slist + tags).

A Lisa Pascal object file starts with the "99 00" magic followed by a
28-byte header; the first table begins right after the header, at offset
0x1C.  The kind is decided from the object magic, the first table at 0x1C,
and (for executables) the presence of a unit table:

    99 00  ...  28-byte header  ...  <first table> at 0x1C
                                            |
                       9c  ->  segment library ->  LIBRARY
                       9d  ->  unit directory  ->  LIBRARY DIRECTORY
                       (otherwise) a valid unit table anywhere ->  EXECUTABLE

Why this is reliable:

  * LIBRARY (9c at 0x1C): the first table is a SEGMENT LIBRARY -- a 9c 00
    marker followed by 28-byte records
    [8-byte segment name][2-byte segment number][8-byte descriptor]
    [10-byte extra].  A library provides code segments/units to be linked
    against (e.g. IOSPASLIB.OBJ, SYS1LIB.OBJ, IOSFPLIB.OBJ, objiolib.obj,
    SULIB.OBJ).

  * LIBRARY DIRECTORY (9d at 0x1C): the intrinsic library (INTRINSIC.LIB).
    It is a directory/index, not a library: it holds a unit directory, a
    segment library and a type table that maps each unit/segment to the
    *library file* that actually contains its code (ObjIOLib.obj,
    IOSPASLIB.OBJ, SYS1LIB.OBJ, ...).  So it is reported in its own
    category and is NOT counted as a library.

  * EXECUTABLE (a valid unit table): the file contains a UNIT TABLE -- a
    9b 00 marker, a 16-bit entry count and a run of 12-byte records
    [8-byte space-padded uppercase unit name][2-byte unit number <= 255]
    [2-byte flag].  find_unit_table() validates every entry, which rejects
    the stray "9b 00" byte pairs that occur in data.  The unit table is the
    robust executable signature: on some volumes it is the first table
    (right at 0x1C), but on others a different table comes first (e.g. a
    small "b2" table) and the unit table sits deeper in the file, so it is
    searched for anywhere.  Libraries also contain unit tables, which is
    why the 9c/9d first-table test is applied first.

The "99 00" magic at offset 0 is necessary but not sufficient (the OS
image, boot and card files also start with it, but carry a different first
table at 0x1C -- e.g. 98 -- and no unit table, so they are reported as
"UNKNOWN").  The unit table, segment table and segment-library record
counts are still computed and printed as corroborating evidence.
"""

import os
import sys

# Make the repository root importable no matter where we are run from.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from LisaFileSystemTool import (
    pascal_to_string,
    to_uint16_big_endian,
    to_uint32_big_endian,
)
from LisaFileSystemToolPerFile import FileSystemWithPerFileCommands
from AnalyzeLisaExecutableFile import (
    find_unit_table,
    find_segment_table,
    find_seg_lib,
)

# Default volume to analyze (relative to the repository root).
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_IMAGE = os.path.join(REPO_ROOT, "LisaSourceCompilation", "LOS_Compilation_Base.image")

# The object-file magic and the first-table type markers (see the module
# docstring for what each means).
OBJ_MAGIC = b"\x99\x00"
FIRST_TABLE_OFFSET = 0x1C  # first table starts right after the 28-byte header
TBL_UNIT = 0x9B            # unit-table marker (the executable signature; found anywhere)
TBL_SEG_LIB = 0x9C         # segment library   -> library (first table at 0x1C)
TBL_LIB_DIR = 0x9D         # unit directory    -> library directory (INTRINSIC.LIB, first table at 0x1C)

KIND_TEXT = "text"
KIND_EXECUTABLE = "executable"
KIND_LIBRARY = "library"
KIND_LIB_DIR = "library directory"
KIND_NOT_OBJECT = "UNKNOWN"

# Ground truth stated by the task (compared case-insensitively).
KNOWN_EXECUTABLE = ("EDITOR.OBJ", "PASCAL.OBJ", "DUMPOBJ.OBJ")
KNOWN_LIBRARY = ("IOSPASLIB.OBJ", "SYS1LIB.OBJ")
KNOWN_LIB_DIR = ("INTRINSIC.LIB",)


def list_volume_files(fs) -> list:
    """Return [(sfile, name, size), ...] for every file on the volume.

    Dispatches on the catalog style: flat catalogs (fs_version 14/15) are
    enumerated by walking the slist; B-tree catalogs (fs_version 16/17) are
    enumerated by walking the HFS B-tree catalog (the same traversal the
    "list" command's dump_catalog() uses).
    """
    if fs.is_flat_catalog_volume():
        return _flat_catalog_list_files(fs)
    return _btree_list_files(fs)


def _flat_catalog_list_files(fs) -> list:
    """Enumerate every file on a flat-catalog (fs_version 14/15) volume by
    walking the slist and re-locating each file's hint page; the same
    mechanics the "list" command uses.
    """
    first, last = fs._flat_catalog_sfile_range()
    rows = []
    for s_file_id in range(first, last + 1):
        entry = fs._slist_entry(s_file_id)
        if entry is None:
            continue
        hintaddr, _fileaddr, filesize, _version = entry
        if hintaddr == 0:
            continue  # unused slist slot
        hint_sector_number = fs._mddf_sector_number + hintaddr
        # The slist hintaddr can be stale; verify/relocate via the tag.
        hint_sector_number = fs._locate_hint_page_for_sfile(s_file_id, hint_sector_number)
        if hint_sector_number is None:
            continue  # no valid hint page exists for this sfile
        name = pascal_to_string(fs.read_sector(hint_sector_number), start=0)
        if not name or "\x00" in name:
            continue  # stale/corrupt hint sector
        rows.append((s_file_id, name, filesize))
    rows.sort(key=lambda r: r[1].lower())
    return rows


def _btree_list_files(fs) -> list:
    """Enumerate every file on a B-tree-catalog (fs_version 16/17) volume.

    Mirrors the traversal in LisaFileSystemTool.dump_catalog(): descend from
    the root node to the leftmost leaf, then walk the chain of leaf nodes and
    parse each record. Only FILEENTRY records (eType 3) are returned, as
    (s_file_id, name, size). The key is
    [0x24][parent ID (2)][name (up to 32 bytes, zero padded)][0x00]; the name
    is exactly the (possibly path-like) name stored in the key, as the
    "list" command prints it. Node layout (2048 bytes = 4 sectors): records
    from offset 0, the offset table (record i's start at 2034 - 2*i), and the
    NodeDesc at 2036 (nkeys), 2042 (next) and 2046 (kind: 0=leaf, 1=index).
    """
    mddf = fs._mddf_sector_number
    NODE = 4 * 512  # a catalog node spans 4 consecutive sectors (2048 bytes)
    KIND_OFF = NODE - 2      # NodeDesc.kind: 0 = leaf, 1 = index
    NKEYS_OFF = NODE - 12    # NodeDesc.nkeys
    NEXT_OFF = NODE - 6      # NodeDesc.next (MDDF-relative page number)
    OFFTAB_BASE = NODE - 14  # record i's start offset is stored at OFFTAB_BASE - 2*i

    def kind(nb): return nb[KIND_OFF]
    def nkeys(nb): return to_uint16_big_endian(nb, NKEYS_OFF)
    def next_page(nb): return to_uint32_big_endian(nb, NEXT_OFF)
    def rec_start(nb, i): return to_uint16_big_endian(nb, OFFTAB_BASE - 2 * i)
    def rec_end(nb, i): return to_uint16_big_endian(nb, OFFTAB_BASE - 2 * (i + 1))

    # Step 1: descend from the root to the leftmost leaf node.
    current_page = fs.find_catalog_root_page_sector_number()
    visited = set()
    while current_page not in visited:
        visited.add(current_page)
        nb = fs.read_4_sectors(mddf + current_page)
        if kind(nb) == 0:  # leaf node
            break
        # Index node: record 0 holds the MDDF-relative page of the leftmost child.
        child = to_uint32_big_endian(nb, to_uint16_big_endian(nb, OFFTAB_BASE))
        if child == current_page or child == 0xFFFFFFFF:
            return []
        current_page = child

    # Step 2: walk the leaf chain, collecting FILEENTRY records.
    rows = []
    seen = set()
    while current_page not in seen:
        seen.add(current_page)
        nb = fs.read_4_sectors(mddf + current_page)
        if kind(nb) != 0:
            break  # not a leaf node; stop
        for i in range(nkeys(nb)):
            rs, re = rec_start(nb, i), rec_end(nb, i)
            if rs == 0xFFFF or re <= rs:
                continue  # bad offset-table entry
            rec = nb[rs:re]
            if len(rec) < 52 or rec[0] != 0x24:
                continue  # not a well-formed catalog key
            etype = (to_uint16_big_endian(rec, 36) >> 8) & 0xFF
            if etype != 3:
                continue  # only FILEENTRY (3); skip directory/thread/other
            name_bytes = rec[3:35]
            null_index = name_bytes.find(b"\x00")
            name = (name_bytes[:null_index] if null_index != -1 else name_bytes).decode(
                "cp1252", errors="replace"
            )
            if not name:
                continue
            s_file_id = to_uint16_big_endian(rec, 38)
            file_size = to_uint32_big_endian(rec, 48)
            rows.append((s_file_id, name, file_size))
        np = next_page(nb)
        if np == 0xFFFFFFFF or np == 0 or np == current_page:
            break  # end of the leaf chain
        current_page = np

    rows.sort(key=lambda r: r[1].lower())
    return rows


def classify(data: bytes, name: str = "") -> dict:
    """Classify a file. Returns a dict with the kind and the supporting
    byte-level evidence.

    A file whose name ends in ".TEXT" (case-insensitive) is classified as
    TEXT regardless of its bytes.  Otherwise the kind is decided by the
    object-file magic plus the first-table type marker at offset 0x1C (see
    the module docstring). The unit table, segment table and
    segment-library record counts are computed as corroborating evidence.
    """
    magic = data[:2]
    first_table = data[FIRST_TABLE_OFFSET] if len(data) > FIRST_TABLE_OFFSET else None

    marker, count = find_unit_table(data)
    seg_start, segs = find_segment_table(data)
    _seglib_marker, _seglib_dir, seglib_recs = find_seg_lib(data)
    has_paslib1 = any((e[2] & 0x1FFF) == 17 for e in segs)

    if name.upper().endswith(".TEXT"):
        kind = KIND_TEXT
    elif magic == OBJ_MAGIC and first_table == TBL_SEG_LIB:
        kind = KIND_LIBRARY
    elif magic == OBJ_MAGIC and first_table == TBL_LIB_DIR:
        kind = KIND_LIB_DIR
    elif magic == OBJ_MAGIC and marker is not None:
        kind = KIND_EXECUTABLE
    else:
        kind = KIND_NOT_OBJECT

    return {
        "magic": magic.hex() if len(magic) >= 2 else "----",
        "first_table": format(first_table, "02x") if first_table is not None else "--",
        "kind": kind,
        "unit_count": count if marker is not None else None,
        "seg_count": len(segs) if seg_start is not None else None,
        "seglib_records": len(seglib_recs),
        "has_paslib1": has_paslib1,
    }


def analyze(image_path: str) -> int:
    """Analyze every file on the volume, print a report, and run the
    ground-truth checks. Returns a process exit code (0 = all checks pass).
    """
    print("=" * 82)
    print("Lisa object-file analyzer (byte-level classification)")
    print(f"Volume: {image_path}")
    print("=" * 82)
    print(
        "Rule: a file named *.TEXT is TEXT.  Otherwise, for a '99 00' object\n"
        "file: first table 9c at 0x1C -> LIBRARY (segment library); 9d ->\n"
        "LIBRARY DIRECTORY (intrinsic library index); otherwise a valid unit\n"
        "table (9b 00, searched anywhere) -> EXECUTABLE.  The unit and segment\n"
        "table counts are printed as corroborating evidence.\n"
    )

    fs = FileSystemWithPerFileCommands(image_path)
    files = list_volume_files(fs)

    print(f"{'NAME':<26}{'SIZE':>9}  {'MAGIC':<6}{'TBL':<5}{'UNITS':>6}{'SEGS':>6}  KIND")
    print("-" * 82)

    results = {}  # name.upper() -> (name, size, classify-dict)
    for sfile, name, size in files:
        try:
            data = fs.flat_catalog_read_file_data(sfile)
        except Exception as e:  # unreadable file: report it, keep going
            print(f"{name:<26}{size:>9}  {'----':<6}{'--':<5}{'?':>6}{'?':>6}  READ-ERROR ({e})")
            results[name.upper()] = (name, size, {"kind": KIND_NOT_OBJECT, "error": str(e)})
            continue
        c = classify(data, name)
        units = str(c["unit_count"]) if c["unit_count"] is not None else "-"
        segs = str(c["seg_count"]) if c["seg_count"] is not None else "-"
        print(f"{name:<26}{size:>9}  {c['magic']:<6}{c['first_table']:<5}{units:>6}{segs:>6}  {c['kind']}")
        results[name.upper()] = (name, size, c)

    # ---- summary -------------------------------------------------------
    names = [v[0] for v in results.values()]
    kinds = {v[0].upper(): v[2]["kind"] for v in results.values()}

    def kind_of(name: str) -> str:
        return kinds[name.upper()]

    text_kind_names = [n for n in names if kind_of(n) == KIND_TEXT]
    exec_names = [n for n in names if kind_of(n) == KIND_EXECUTABLE]
    lib_names = [n for n in names if kind_of(n) == KIND_LIBRARY]
    libdir_names = [n for n in names if kind_of(n) == KIND_LIB_DIR]
    other_names = [n for n in names if kind_of(n) == KIND_NOT_OBJECT]
    text_ext_names = [n for n in names if n.upper().endswith(".TEXT")]
    obj_names = [n for n in names if n.upper().endswith(".OBJ")]
    obj_exec = [n for n in exec_names if n.upper().endswith(".OBJ")]

    print("-" * 82)
    print(f"Total files:              {len(results)}")
    print(f"Text (.TEXT):             {len(text_kind_names)}")
    print(f"Executable:               {len(exec_names)}")
    print(f"Library:                  {len(lib_names)}")
    print(f"Library directory:        {len(libdir_names)}   ({', '.join(sorted(libdir_names)) or '-'})")
    print(f"UNKNOWN:                  {len(other_names)}")
    print(f".OBJ files:               {len(obj_names)}  "
          f"({len(obj_exec)} executable, "
          f"{sum(1 for n in obj_names if kind_of(n) == KIND_LIBRARY)} library)")

    # ---- ground-truth checks ------------------------------------------
    print("-" * 82)
    print("Ground-truth checks:")
    checks = []

    def check(label: str, ok: bool) -> None:
        checks.append(ok)
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")

    for known in KNOWN_EXECUTABLE:
        check(f"{known} is executable", kinds.get(known.upper()) == KIND_EXECUTABLE)
    for known in KNOWN_LIBRARY:
        check(f"{known} is a library", kinds.get(known.upper()) == KIND_LIBRARY)
    for known in KNOWN_LIB_DIR:
        check(
            f"{known} is a library directory (not a library)",
            kinds.get(known.upper()) == KIND_LIB_DIR,
        )

    text_not_text = [n for n in text_ext_names if kind_of(n) != KIND_TEXT]
    check(
        f"all {len(text_ext_names)} .TEXT files are classified as text",
        not text_not_text,
    )
    if text_not_text:
        print(f"        (offenders: {', '.join(sorted(text_not_text))})")

    if obj_names:
        ratio = len(obj_exec) / len(obj_names)
        check(
            f"most .OBJ files are executable ({len(obj_exec)}/{len(obj_names)} = {ratio:.0%})",
            ratio > 0.5,
        )

    all_ok = all(checks)
    print("-" * 82)
    print("RESULT: " + ("ALL CHECKS PASSED" if all_ok else "SOME CHECKS FAILED"))
    return 0 if all_ok else 1


def main() -> None:
    image_path = sys.argv[1] if len(sys.argv) >= 2 else DEFAULT_IMAGE
    if not os.path.exists(image_path):
        print(f"ERROR: disk image not found: {image_path}")
        sys.exit(2)
    try:
        sys.exit(analyze(image_path))
    except FileNotFoundError:
        print(f"File {image_path} not found!")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e} while analyzing {image_path}!")
        sys.exit(1)


if __name__ == "__main__":
    main()
