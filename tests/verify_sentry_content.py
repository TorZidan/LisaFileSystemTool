#!/usr/bin/env python3
import os, struct, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from LisaFileSystemTool import InMemoryFileSystem, pascal_to_string

IMG = "/tmp/lisaem-profile-5MB-FreshInstallOf_PascalWorkshop1.0.dc42"
fs = InMemoryFileSystem(IMG)
mddf = fs._mddf_sector_number

slist_addr = struct.unpack(">I", fs.read_sector(mddf)[0x94:0x98])[0]
slist_packing = struct.unpack(">H", fs.read_sector(mddf)[0x98:0x9A])[0]
print(f"slist_addr={slist_addr}, slist_packing={slist_packing}")

def sentry_raw(sid):
    spage = sid // slist_packing
    soffs = (sid % slist_packing) * 14
    page = fs.read_sector(mddf + slist_addr + spage)
    return page[soffs:soffs+14]

for sid, nm in ((6, "Diff.obj"), (93, "Diff2.obj")):
    raw = sentry_raw(sid)
    hintaddr, fileaddr, filesize = struct.unpack(">III", raw[0:12])
    version = struct.unpack(">H", raw[12:14])[0]
    # tag version of first data page
    tag = fs.read_tags_for_sector(mddf + fileaddr)
    tagver = struct.unpack(">H", tag[0:2])[0]
    print(f"{nm} sfile={sid}: sentry=hintaddr {hintaddr}, fileaddr {fileaddr}, filesize {filesize}, sentry.version={version}  |  first-datapage tag.version={tagver}  -> {'MATCH' if version==tagver else 'MISMATCH!'}")

# ---- full byte comparison of data pages ----
def read_chain(fileaddr, count):
    out = bytearray()
    rel = fileaddr
    for i in range(count):
        out += fs.read_sector(mddf + rel)
        rel += 1
    return bytes(out)

d1 = read_chain(102, 18)   # Diff.obj
d2 = read_chain(3602, 18)  # Diff2.obj
print(f"\nDiff.obj data  (9216 bytes) md5-ish: {sum(d1):#x} first16={d1[:16].hex(' ')}")
print(f"Diff2.obj data (9216 bytes) md5-ish: {sum(d2):#x} first16={d2[:16].hex(' ')}")
print(f"Data pages identical: {d1 == d2}")

with open("/tmp/dc42-dump/Diff.obj","rb") as f:
    host = f.read()
print(f"Host /tmp/dc42-dump/Diff.obj: {len(host)} bytes, sum={sum(host):#x}")
print(f"Host == Diff.obj on-disk: {host == d1}")
print(f"Host == Diff2.obj on-disk: {host == d2}")
