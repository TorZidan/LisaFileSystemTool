#!/usr/bin/env python3
import os, struct, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from LisaFileSystemTool import InMemoryFileSystem

IMG = "/tmp/lisaem-profile-5MB-FreshInstallOf_PascalWorkshop1.0.dc42"
fs = InMemoryFileSystem(IMG)
mddf = fs._mddf_sector_number

# Diff.obj: hint 100-101, data 102-119 ; Diff2.obj: hint 3314-3315, data 3602-3619
for label, rels in (("Diff.obj", list(range(100, 120))), ("Diff2.obj", list(range(3314, 3316)) + list(range(3602, 3620)))):
    print(f"=== {label} tag checksum verification ===")
    bad = 0
    for rel in rels:
        sn = mddf + rel
        tag = fs.read_tags_for_sector(sn)
        stored = tag[11]
        computed = fs.calculate_new_tag_checksum(sn)
        status = "OK " if stored == computed else "BAD"
        if stored != computed:
            bad += 1
            print(f"  sector {sn} (rel {rel}): stored={stored:#04x} computed={computed:#04x}  {status}")
    print(f"  {len(rels) - bad}/{len(rels)} tag checksums correct")
