#!/usr/bin/env python3
"""Deep analysis of '?' (unknown allocated) sectors in a DC42 image.

For every sector whose tag does not clearly identify it (as in visualize_volume),
this script prints:
  - the full 12-byte tag (version, vol_id, file_id, rel_num, fwd/dataused, bwd/dataused)
  - the allocation-bitmap state
  - the first bytes of the sector data
and cross-checks:
  - slist entries (hintaddr/fileaddr) that point at such sectors
  - per-file: number of tagged data sectors vs. the slist filesize
  - whether the '?' sectors are contiguous with a known file's data/hint region
  - the catalog (B-tree) file list, to see which files the catalog thinks exist
"""

import struct
import sys

sys.path.insert(0, "/home/ivo/github/LisaFileSystemTool")
from LisaFileSystemTool import (
    InMemoryFileSystem,
    pascal_to_string,
    to_uint16_big_endian,
    to_uint32_big_endian,
)

IMAGE = sys.argv[1] if len(sys.argv) > 1 else "/home/ivo/Downloads/BaCISPlus/good.dc42"

t = InMemoryFileSystem(IMAGE)
mddf = t._mddf_sector_number
mddf_bytes = t._mddf_sector_bytes
n = t._num_sectors

print("\n" + "=" * 78)
print("STEP 1: MDDF key fields")
print("=" * 78)
fields = [
    ("bitmap_addr (0x88)", 0x88, 4),
    ("bitmap_size (0x92)", 0x92, 2),
    ("slist_addr (0x94)", 0x94, 4),
    ("slist_packing (0x98)", 0x98, 2),
    ("slist_block_count (0x9A)", 0x9A, 2),
    ("first_file (0x9C)", 0x9C, 2),
    ("empty_file (0x9E)", 0x9E, 2),
    ("maxfiles (0xA0)", 0xA0, 2),
    ("filecount (0xB0)", 0xB0, 2),
    ("freecount (0xBA)", 0xBA, 2),
    ("catalog root (0x12E)", 0x12E, 4),
    ("hentry_offset (0x116)", 0x116, 2),
]
for name, off, sz in fields:
    v = (
        to_uint32_big_endian(mddf_bytes, off)
        if sz == 4
        else to_uint16_big_endian(mddf_bytes, off)
    )
    print(f"  {name:24} = {v}")

# ---- allocation bitmap -------------------------------------------------------
bitmap_start_rel = to_uint32_big_endian(mddf_bytes, 0x88)
num_bitmap_sectors = to_uint16_big_endian(mddf_bytes, 0x92)
bitmap = bytearray()
for i in range(num_bitmap_sectors):
    bitmap += t.read_sector(mddf + bitmap_start_rel + i)

def is_allocated(abs_sector: int) -> bool:
    rel = abs_sector - mddf
    if rel < 0 or rel >= len(bitmap) * 8:
        return False
    return bool(bitmap[rel // 8] & (1 << (rel & 7)))

# ---- slist -------------------------------------------------------------------
print("\n" + "=" * 78)
print("STEP 2: slist entries (all files)")
print("=" * 78)
slist_addr = to_uint32_big_endian(mddf_bytes, 0x94)
slist_packing = to_uint16_big_endian(mddf_bytes, 0x98)
slist_block_count = to_uint16_big_endian(mddf_bytes, 0x9A)
slist = {}  # s_file_id -> (hintaddr, fileaddr, filesize, version)
for page in range(slist_block_count):
    table = t.read_sector(mddf + slist_addr + page)
    for idx in range(slist_packing):
        sid = page * slist_packing + idx
        if sid == 0:
            continue
        off = idx * 14
        hintaddr = to_uint32_big_endian(table, off)
        fileaddr = to_uint32_big_endian(table, off + 4)
        filesize = to_uint32_big_endian(table, off + 8)
        version = to_uint16_big_endian(table, off + 12)
        if hintaddr == 0 and fileaddr == 0 and filesize == 0:
            continue
        slist[sid] = (hintaddr, fileaddr, filesize, version)

def name_of(sid):
    e = slist.get(sid)
    if not e:
        return None
    hintaddr = e[0]
    if 0 < hintaddr < 0x7FFFFFFF:
        try:
            return pascal_to_string(t.read_sector(mddf + hintaddr), start=0)
        except Exception:
            return None
    return None

for sid in sorted(slist):
    hintaddr, fileaddr, filesize, version = slist[sid]
    nm = name_of(sid)
    print(
        f"  s_file_id {sid:3}: name={nm!r:35} hintaddr={hintaddr:5} fileaddr={fileaddr:5} filesize={filesize:6} version={version}"
    )

# ---- tags of all sectors -----------------------------------------------------
print("\n" + "=" * 78)
print("STEP 3: per-sector tags of interest (file_id not 0/0x7FFF, plus all '?')")
print("=" * 78)

tags = []
for i in range(n):
    tb = t.read_tags_for_sector(i)
    fid = struct.unpack(">H", tb[4:6])[0]
    rel_num = struct.unpack(">H", tb[6:8])[0]
    fwdb = tb[8:10]
    bwdb = tb[10:12]
    fwd = (fwdb[0] << 1) | (fwdb[1] >> 7)          # 11 bits
    fwd_du = fwdb[1] & 0x7F                          # 5 bits (last sector only, scaled)
    bwd = (bwdb[0] << 1) | (bwdb[1] >> 7)
    bwd_du = bwdb[1] & 0x7F
    tags.append((i, tb, fid, rel_num, fwd, fwd_du, bwd, bwd_du))

# sectors tagged with each file id
fid_to_sectors = {}
for i, tb, fid, rel, fwd, fdu, bwd, bdu in tags:
    fid_to_sectors.setdefault(fid, []).append(i)

known_fids = set(slist.keys())
print("  Distinct file_ids found in tags:")
for fid in sorted(fid_to_sectors):
    secs = fid_to_sectors[fid]
    kind = ""
    if fid in (0, 0x7FFF):
        kind = "(free/erased)"
    elif fid == 0x0001:
        kind = "(MDDF)"
    elif fid == 0x0002:
        kind = "(bitmap)"
    elif fid == 0x0003:
        kind = "(slist)"
    elif fid == 0x0004:
        kind = "(catalog)"
    elif fid == 0xAAAA:
        kind = "(boot)"
    elif fid == 0xBBBB:
        kind = "(loader)"
    elif fid >= 0x8000:
        kind = f"(hint page of s_file_id {0x10000 - fid})"
        if (0x10000 - fid) in known_fids:
            kind += " [KNOWN]"
        else:
            kind += " [NOT IN SLIST]"
    elif fid in known_fids:
        kind = f"(data of s_file_id {fid})"
    else:
        kind = "(data of file NOT IN SLIST)"
    rng = f"{secs[0]}..{secs[-1]}" if len(secs) > 1 else f"{secs[0]}"
    print(f"    file_id {fid:#06x}: {len(secs):3} sector(s) {rng:12} {kind}")

# ---- '?' sectors: full detail ------------------------------------------------
print("\n" + "=" * 78)
print("STEP 4: full detail of every '?' sector")
print("=" * 78)

def classify(i):
    """Replicates visualize_volume's classification; returns the char."""
    fid = tags[i][2]
    if fid == 0x0001: return "M"
    if fid == 0x0002: return "P"
    if fid == 0x0003: return "S"
    if fid == 0x0004: return "C"
    if fid == 0xAAAA: return "B"
    if fid == 0xBBBB: return "L"
    if fid in (0x0000, 0x7FFF):
        return "?" if is_allocated(i) else "."
    if fid >= 0x8000:
        return "H" if (0x10000 - fid) in known_fids else ("?" if is_allocated(i) else ".")
    if fid in known_fids:
        return "1"
    return "?" if is_allocated(i) else "."

q_sectors = [i for i in range(n) if classify(i) == "?"]
print(f"  '?' sectors: {q_sectors}\n")
for i in q_sectors:
    i, tb, fid, rel, fwd, fdu, bwd, bdu = tags[i]
    data = t.read_sector(i)
    print(
        f"  sector {i:3}: tag={tb.hex(' ')}  bitmap={'ALLOCATED' if is_allocated(i) else 'free'}"
    )
    print(f"           file_id={fid:#06x} rel_num={rel} fwd={fwd:#05x} fwd_du={fdu} bwd={bwd:#05x} bwd_du={bdu}")
    print(f"           data[0:32]={data[:32].hex(' ')}")
    nz = sum(1 for b in data if b)
    print(f"           non-zero bytes in sector: {nz}/512")
    print()

# ---- cross-checks ------------------------------------------------------------
print("=" * 78)
print("STEP 5: cross-checks")
print("=" * 78)

# 5a: slist entries pointing at '?' sectors
print("\n  5a. slist entries whose hintaddr/fileaddr land on a '?' sector:")
found = False
for sid in sorted(slist):
    hintaddr, fileaddr, filesize, version = slist[sid]
    for label, a in (("hintaddr", hintaddr), ("fileaddr", fileaddr)):
        absa = mddf + a
        if 0 < a < 0x7FFFFFFF and absa in q_sectors:
            found = True
            print(f"    s_file_id {sid} ({name_of(sid)!r}): {label}={a} -> sector {absa} is '?'")
if not found:
    print("    (none)")

# 5b: per-file tagged-sector count vs filesize
print("\n  5b. per-file: tagged data sectors vs slist filesize:")
for sid in sorted(slist):
    hintaddr, fileaddr, filesize, version = slist[sid]
    data_secs = fid_to_sectors.get(sid, [])
    hint_secs = fid_to_sectors.get((0x10000 - sid) % 0x10000, [])
    cap = len(data_secs) * 512
    status = "OK" if filesize <= cap else f"SHORT (have {cap} bytes)"
    print(
        f"    s_file_id {sid:3} ({name_of(sid)!r:35}): filesize={filesize:6} "
        f"tagged-data-sectors={len(data_secs)} ({cap} B) hint-pages={len(hint_secs)} : {status}"
    )

# 5c: are '?' sectors contiguous with a known file's region?
print("\n  5c. neighbourhood of each '?' sector (tag file_id of neighbours):")
for i in q_sectors:
    left = tags[i - 1][2] if i > 0 else None
    right = tags[i + 1][2] if i + 1 < n else None
    la = is_allocated(i - 1) if i > 0 else None
    ra = is_allocated(i + 1) if i + 1 < n else None
    print(
        f"    sector {i:3}: left={left:#06x}({'alloc' if la else 'free'})  right={right:#06x}({'alloc' if ra else 'free'})"
    )

# 5d: bitmap vs tags global consistency
print("\n  5d. global bitmap/tag consistency:")
alloc_by_tag = []
alloc_by_bitmap = []
for i in range(n):
    fid = tags[i][2]
    tag_alloc = fid not in (0x0000, 0x7FFF)
    bm_alloc = is_allocated(i)
    if tag_alloc:
        alloc_by_tag.append(i)
    if bm_alloc:
        alloc_by_bitmap.append(i)
only_bitmap = sorted(set(alloc_by_bitmap) - set(alloc_by_tag))
only_tag = sorted(set(alloc_by_tag) - set(alloc_by_bitmap))
print(f"    allocated in bitmap only (tag says free): {only_bitmap}")
print(f"    allocated in tag only (bitmap says free): {only_tag}")
print(f"    bitmap allocated total: {len(alloc_by_bitmap)}; MDDF freecount field: {to_uint16_big_endian(mddf_bytes, 0xBA)}")

# 5e: catalog dump (which files does the B-tree catalog list?)
print("\n  5e. catalog files (from dump_catalog):")
try:
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        t.dump_catalog()
    print(buf.getvalue()[:4000])
except Exception as e:
    print(f"    (dump_catalog failed: {e})")
