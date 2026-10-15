#!/usr/bin/env python3
import os, struct, sys, collections
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from LisaFileSystemTool import InMemoryFileSystem, pascal_to_string

IMG = "/tmp/lisaem-profile-5MB-FreshInstallOf_PascalWorkshop1.0.dc42"
fs = InMemoryFileSystem(IMG)
mddf = fs._mddf_sector_number
n = fs._num_sectors

slist_addr = struct.unpack(">I", fs.read_sector(mddf)[0x94:0x98])[0]
slist_packing = struct.unpack(">H", fs.read_sector(mddf)[0x98:0x9A])[0]
first, last = fs._flat_catalog_sfile_range()

# sentry version per sfile
sentries = {}
for sid in range(1, 400):
    spage = sid // slist_packing
    soffs = (sid % slist_packing) * 14
    page = fs.read_sector(mddf + slist_addr + spage)
    hintaddr, fileaddr, filesize = struct.unpack(">III", page[soffs:soffs+12])
    ver = struct.unpack(">H", page[soffs+12:soffs+14])[0]
    sentries[sid] = (hintaddr, fileaddr, filesize, ver)

# For every page tagged with fid>0 (data) or fid<0 (hint), compare tag version to sentry version
mismatch_data = 0
mismatch_hint = 0
total_data = 0
total_hint = 0
vol_by_kind = collections.Counter()
for s in range(n):
    tag = fs.read_tags_for_sector(s)
    ver = struct.unpack(">H", tag[0:2])[0]
    vol = struct.unpack(">H", tag[2:4])[0]
    fid = struct.unpack(">H", tag[4:6])[0]
    if 1 <= fid < 0x8000:
        total_data += 1
        vol_by_kind[("data", vol)] += 1
        se = sentries.get(fid)
        if se and se[3] != ver:
            mismatch_data += 1
            if mismatch_data <= 10:
                print(f"DATA mismatch: sector {s} fid={fid} tag.ver={ver} sentry.ver={se[3]}")
    elif fid >= 0x8000:
        sid = 0x10000 - fid
        total_hint += 1
        vol_by_kind[("hint", vol)] += 1
        se = sentries.get(sid)
        if se and se[3] != ver:
            mismatch_hint += 1
            if mismatch_hint <= 10:
                print(f"HINT mismatch: sector {s} sid={sid} tag.ver={ver} sentry.ver={se[3]}")

print(f"\nDATA pages: {total_data} total, {mismatch_data} tag/sentry version mismatches")
print(f"HINT pages: {total_hint} total, {mismatch_hint} tag/sentry version mismatches")
print("\nvolume field distribution:")
for (kind, vol), c in sorted(vol_by_kind.items()):
    print(f"  {kind}: vol={vol:#06x}: {c}")
