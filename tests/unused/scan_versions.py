#!/usr/bin/env python3
import os, struct, sys, collections
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from LisaFileSystemTool import InMemoryFileSystem

IMG = "/tmp/lisaem-profile-5MB-FreshInstallOf_PascalWorkshop1.0.dc42"
fs = InMemoryFileSystem(IMG)
n = fs._num_sectors

ver_count = collections.Counter()
vol_count = collections.Counter()
# For file data pages (fileid > 0 and != special), collect version distribution
data_ver = collections.Counter()
hint_ver = collections.Counter()
free_ver = collections.Counter()
for s in range(n):
    tag = fs.read_tags_for_sector(s)
    ver, vol, fid = struct.unpack(">HHH", tag[0:6])
    ver_count[ver] += 1
    vol_count[vol] += 1
    if fid == 0:
        free_ver[ver] += 1
    elif fid > 0:
        data_ver[ver] += 1
    else:  # negative -> hint page
        hint_ver[ver] += 1

print("version field distribution (all sectors):")
for v, c in sorted(ver_count.items()):
    print(f"  version={v:#06x}: {c}")
print("vol_id field distribution (all sectors):")
for v, c in sorted(vol_count.items()):
    print(f"  vol={v:#06x}: {c}")
print(f"version among DATA pages (fileid>0):  {dict(data_ver)}")
print(f"version among HINT pages (fileid<0):  {dict(hint_ver)}")
print(f"version among FREE pages (fileid=0):  {dict(free_ver)}")
