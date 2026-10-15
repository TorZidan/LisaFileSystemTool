#!/usr/bin/env python3
import os, struct, sys, collections
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from LisaFileSystemTool import InMemoryFileSystem, pascal_to_string

IMG = "/tmp/lisaem-profile-5MB-FreshInstallOf_PascalWorkshop1.0.dc42"
fs = InMemoryFileSystem(IMG)
mddf = fs._mddf_sector_number
n = fs._num_sectors

# group data-page version by s-file id
by_fid = collections.defaultdict(collections.Counter)
for s in range(n):
    tag = fs.read_tags_for_sector(s)
    ver = struct.unpack(">H", tag[0:2])[0]
    fid = struct.unpack(">H", tag[4:6])[0]
    if 0 < fid <= 0x7FFF:
        by_fid[fid][ver] += 1

# map sfile id to name
first, last = fs._flat_catalog_sfile_range()
names = {}
for sid in range(first, last + 1):
    e = fs._slist_entry(sid)
    if e is None or e[0] == 0:
        continue
    hs = fs.read_sector(mddf + e[0])
    names[sid] = pascal_to_string(hs, start=0)

print("sfiles with data pages and their tag-version distribution:")
for fid in sorted(by_fid):
    nm = names.get(fid, "<unknown>")
    print(f"  sfile {fid:3d} '{nm}': versions={dict(by_fid[fid])}")
