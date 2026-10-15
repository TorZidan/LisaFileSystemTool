#!/usr/bin/env python3
import os, struct, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from LisaFileSystemTool import InMemoryFileSystem

IMG = "/tmp/lisaem-profile-5MB-FreshInstallOf_PascalWorkshop1.0.dc42"
fs = InMemoryFileSystem(IMG)
mddf = fs._mddf_sector_number
n = fs._num_sectors

for s in range(n):
    tag = fs.read_tags_for_sector(s)
    ver = struct.unpack(">H", tag[0:2])[0]
    fid = struct.unpack(">H", tag[4:6])[0]
    if 0 < fid <= 0x7FFF and fid in (93, 94, 95, 99, 101):
        used = struct.unpack(">H", tag[6:8])[0]
        absp = (tag[8] << 16) | (tag[9] << 8) | tag[10]
        relp = struct.unpack(">H", tag[12:14])[0]
        fwd = (tag[14] << 16) | (tag[15] << 8) | tag[16]
        bkw = (tag[17] << 16) | (tag[18] << 8) | tag[19]
        print(f"sector {s:5d} (rel {s - mddf:5d}): {tag.hex(' ')}")
        print(f"    ver={ver} fid={fid} used={used:#06x} absp={absp:#06x} relp={relp} fwd={fwd:#08x} bkw={bkw:#08x}")
        # print first 32 bytes of data
        d = fs.read_sector(s)
        print(f"    data[0:32] = {d[:32].hex(' ')}")
