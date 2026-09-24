#!/usr/bin/env python3
import os, struct, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from LisaFileSystemTool import InMemoryFileSystem, pascal_to_string

IMG = "/tmp/lisaem-profile-5MB-FreshInstallOf_PascalWorkshop1.0.dc42"
fs = InMemoryFileSystem(IMG)
mddf = fs._mddf_sector_number

def find_sfile(name):
    first, last = fs._flat_catalog_sfile_range()
    for sid in range(first, last + 1):
        e = fs._slist_entry(sid)
        if e is None or e[0] == 0:
            continue
        hs = fs.read_sector(mddf + e[0])
        if pascal_to_string(hs, start=0) == name:
            return sid, e
    return None, None

for nm in ("Diff.obj", "Diff2.obj"):
    sid, e = find_sfile(nm)
    fs.print_hint_sector_info(mddf + e[0])
    # also print the full raw hentry bytes
    d = fs.read_sector(mddf + e[0])
    print("  RAW hentry:")
    for i in range(0, 128, 16):
        print(f"    {i:02x}: {d[i:i+16].hex(' ')}")
    print()
