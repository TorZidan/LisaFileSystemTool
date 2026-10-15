#!/usr/bin/env python3
import os, struct, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from LisaFileSystemTool import InMemoryFileSystem, pascal_to_string

IMG = "/tmp/lisaem-profile-5MB-FreshInstallOf_PascalWorkshop1.0.dc42"
fs = InMemoryFileSystem(IMG)
mddf = fs._mddf_sector_number

# MDDF fields
m = fs.read_sector(mddf)
print(f"MDDF: hintsize(0xA2)={struct.unpack('>H', m[0xA2:0xA4])[0]}, "
      f"map_offset(0xAC)={struct.unpack('>H', m[0xAC:0xAE])[0]}, "
      f"empty_file(0x9E)={struct.unpack('>H', m[0x9E:0xA0])[0]}, "
      f"filecount(0xB0)={struct.unpack('>H', m[0xB0:0xB2])[0]}, "
      f"maxfiles(0xA0)={struct.unpack('>H', m[0xA0:0xA2])[0]}")

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

def show(name):
    sid, e = find_sfile(name)
    hintaddr, fileaddr, filesize, _ = e
    print(f"\n=== {name} sfile={sid}: hintaddr={hintaddr}, fileaddr={fileaddr}, filesize={filesize} ===")
    # dump hint pages (2 pages: hentry + smallmap)
    for i in range(2):
        rel = hintaddr + i
        d = fs.read_sector(mddf + rel)
        tag = fs.read_tags_for_sector(mddf + rel)
        print(f"  hint page {i} rel={rel}: tag={tag.hex(' ')}")
        print(f"    data[0:64]={d[:64].hex(' ')}")
        if i == 1:
            size = struct.unpack(">I", d[0:4])[0]
            maxe = struct.unpack(">H", d[4:6])[0]
            ec = struct.unpack(">H", d[6:8])[0]
            print(f"    SMALLMAP: size={size}, max_entries={maxe}, ecount={ec}")
            for r in range(ec):
                st = struct.unpack(">I", d[8 + r * 6: 12 + r * 6])[0]
                ct = struct.unpack(">H", d[12 + r * 6: 14 + r * 6])[0]
                print(f"      run[{r}]: start={st} (abs {mddf + st}), count={ct}")

for nm in ("Diff.obj", "Diff2.obj"):
    show(nm)
