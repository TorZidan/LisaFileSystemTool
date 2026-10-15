#!/usr/bin/env python3
import os, struct, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from LisaFileSystemTool import InMemoryFileSystem, pascal_to_string

IMG = "/tmp/lisaem-profile-5MB-FreshInstallOf_PascalWorkshop1.0.dc42"
fs = InMemoryFileSystem(IMG)
mddf = fs._mddf_sector_number

slist_addr = struct.unpack(">I", fs.read_sector(mddf)[0x94:0x98])[0]
slist_packing = struct.unpack(">H", fs.read_sector(mddf)[0x98:0x9A])[0]

def sentry(sid):
    spage = sid // slist_packing
    soffs = (sid % slist_packing) * 14
    page = fs.read_sector(mddf + slist_addr + spage)
    h, f, sz = struct.unpack(">III", page[soffs:soffs+12])
    v = struct.unpack(">H", page[soffs+12:soffs+14])[0]
    return h, f, sz, v

def show(sid, name):
    h, f, sz, v = sentry(sid)
    ht = fs.read_tags_for_sector(mddf + h)
    dt = fs.read_tags_for_sector(mddf + f) if f else None
    hver, hvol = struct.unpack(">HH", ht[0:4])
    dver = dvol = None
    if dt:
        dver, dvol = struct.unpack(">HH", dt[0:4])
    print(f"{name:16s} sfile={sid:3d} sentry.ver={v} | hint: ver={hver:#05x} vol={hvol:#06x} | data: ver={dver and hex(dver)} vol={dvol and hex(dvol)}")

# rootcatalog and a few known-good + tool-added files
rc = fs._find_rootcatalog_sfile()
print(f"rootcatalog sfile = {rc}")
show(rc, "rootcatalog")
for sid, nm in ((5,"ByteDiff.obj"),(6,"Diff.obj"),(8,"EDIT.MENUS.TEXT"),
                (26,"SYSTEM.OS"),(49,"Pascal.obj"),(91,"COBOL.OBJ"),(93,"Diff2.obj")):
    show(sid, nm)
