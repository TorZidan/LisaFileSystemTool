#!/usr/bin/env python3
"""Repair the tag `version` field of tool-added files in a flat-catalog dc42 image.

The old LisaFileSystemTool add stamped every new page's tag with version 0
(copied from the rootcatalog) instead of with the slist sentry's version, as the
OS does (pl.version := ptrSent^.version). The driver verifies tag version against
the sentry version on read, so those files are unreadable (Error 132 on run,
copy fails).

This script re-stamps the version field of every page that is reachable through a
file's own hint chain + data chain to match that file's sentry version, recomputes
the affected tag checksums, and fixes the DC42 header checksums.

A backup copy of the image is made first.
"""
import os
import shutil
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from LisaFileSystemTool import InMemoryFileSystem

IMG = sys.argv[1] if len(sys.argv) > 1 else (
    "/tmp/"
    "lisaem-profile-5MB-FreshInstallOf_PascalWorkshop1.0.dc42"
)

shutil.copy2(IMG, IMG + ".pre-repair-backup")
print(f"Backup written: {IMG}.pre-repair-backup")

fs = InMemoryFileSystem(IMG)
mddf = fs._mddf_sector_number
m = fs.read_sector(mddf)
slist_addr = struct.unpack(">I", m[0x94:0x98])[0]
slist_packing = struct.unpack(">H", m[0x98:0x9A])[0]
maxfiles = struct.unpack(">H", m[0xA0:0xA2])[0]
hintsize = struct.unpack(">H", m[0xA2:0xA4])[0]
map_offset = struct.unpack(">H", m[0xAC:0xAE])[0]
END = 0xFFFFFF


def sentry(sid):
    sp = sid // slist_packing
    so = (sid % slist_packing) * 14
    pg = fs.read_sector(mddf + slist_addr + sp)
    return struct.unpack(">IIIH", pg[so:so + 14])


def u32(b, o):
    return (b[o] << 16) | (b[o + 1] << 8) | b[o + 2]


def chain_from(start):
    pages, cur = [], start
    while cur != END and len(pages) < 4096:
        pages.append(cur)
        cur = u32(fs.read_tags_for_sector(mddf + cur), 14)
    return pages


def file_name_of(sid):
    h, f, sz, v = sentry(sid)
    try:
        rec = fs.read_sector(mddf + h)
        ln = rec[0]
        return rec[1:1 + ln].decode("mac-roman", errors="replace")
    except Exception:
        return "?"


modified = set()
total_fixed = 0
for sid in range(1, maxfiles):
    h, f, sz, v = sentry(sid)
    if h == 0:
        continue
    expected = []  # (abs_sector, expected_fileid)
    for p in chain_from(h)[:hintsize]:
        expected.append((mddf + p, (0x10000 - sid) & 0xFFFF))
    if f != 0:
        for p in chain_from(f):
            expected.append((mddf + p, sid))
    file_fixed = 0
    for absn, exp_fid in expected:
        t = bytearray(fs.read_tags_for_sector(absn))
        fid = (t[4] << 8) | t[5]
        if fid != exp_fid:
            print(f"  WARNING: sfile {sid} ({file_name_of(sid)}): sector {absn} "
                  f"tag fileid {fid:#06x} != expected {exp_fid:#06x}; skipped")
            continue
        curv = (t[0] << 8) | t[1]
        if curv != v:
            struct.pack_into(">H", t, 0, v & 0xFFFF)
            fs._mem_write_sector_tag(absn, bytes(t))
            modified.add(absn)
            file_fixed += 1
    if file_fixed:
        total_fixed += file_fixed
        print(f"  sfile {sid:3d} '{file_name_of(sid)}': re-stamped {file_fixed} "
              f"page(s) with sentry version {v}")

if not modified:
    print("Nothing to repair: all reachable page tags already match their sentry version.")
    sys.exit(0)

print(f"\nRe-stamping {len(modified)} sector tag(s); recomputing checksums...")
with open(IMG, "r+b") as fw:
    for sn in sorted(modified):
        d = fs.read_sector(sn)
        tg = fs.read_tags_for_sector(sn)
        ck = fs.calculate_new_tag_checksum(sn)
        tg = tg[:11] + bytes([ck]) + tg[12:]
        # keep the in-memory copy in sync (fix_dc42_checksum computes from it!)
        fs._file.seek(fs._sector_tag_file_offset(sn) + 11)
        fs._file.write(bytes([ck]))
        fw.seek(fs._sector_data_file_offset(sn))
        fw.write(d)
        fw.seek(fs._sector_tag_file_offset(sn))
        fw.write(tg)

fs.fix_dc42_checksum(confirm=False)
print(f"Done. Fixed {total_fixed} page tag(s) in total.")
