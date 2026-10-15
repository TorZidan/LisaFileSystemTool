#!/usr/bin/env python3
"""Compare the data-sector tags of Diff.obj and Diff2.obj in the dc42 image."""
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
        hint = mddf + e[0]
        hs = fs.read_sector(hint)
        if pascal_to_string(hs, start=0) == name:
            return sid, e
    return None, None

def dump_tags(sid, name, e):
    hintaddr, fileaddr, filesize, _ = e
    print(f"\n=== {name}: s_file_id={sid}, hintaddr={hintaddr}, fileaddr={fileaddr}, filesize={filesize} ===")
    hs_tag = fs.read_tags_for_sector(mddf + hintaddr)
    print(f"  HINT tag @abs {mddf + hintaddr}: {hs_tag.hex(' ')}")
    rel = fileaddr
    i = 0
    while True:
        tag = fs.read_tags_for_sector(mddf + rel)
        ver, vol, fid, used = struct.unpack(">HHHH", tag[0:8])
        absp = (tag[8] << 16) | (tag[9] << 8) | tag[10]
        relp = struct.unpack(">H", tag[12:14])[0]
        fwd = (tag[14] << 16) | (tag[15] << 8) | tag[16]
        bkw = (tag[17] << 16) | (tag[18] << 8) | tag[19]
        print(f"  DATA[{i}] rel={rel} abs={mddf + rel}: raw={tag.hex(' ')}")
        print(f"      version={ver:#06x} volume={vol:#06x} fileid={fid:#06x} "
              f"dataused={used:#06x} abspage={absp:#08x} cksum={tag[11]:#04x} "
              f"relpage={relp} fwd={fwd:#08x} bkw={bkw:#08x}")
        if fwd == 0xFFFFFF:
            break
        rel = fwd
        i += 1
        if i > 64:
            print("  chain too long, aborting")
            break

for nm in ("Diff.obj", "Diff2.obj"):
    sid, e = find_sfile(nm)
    if e is None:
        print(f"!!! {nm} not found")
        continue
    dump_tags(sid, nm, e)
