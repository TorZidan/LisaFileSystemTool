#!/usr/bin/env python3
"""Verify the DC42 volume allocation map (bitmap) for the added file Diff2.obj.

Checks:
 1. Bitmap fields in the MDDF (addr/size/pages).
 2. For EVERY file on the volume: are all its hint+data pages marked allocated?
 3. Specifically: Diff2.obj (sfile 91) hint/data pages.
 4. Pages marked allocated but not referenced by any file (system vs orphan).
 5. MDDF freecount vs actual free-bit count.
 6. If a reference image is given: which bitmap bits differ.
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from importlib import import_module

mod = import_module("LisaFileSystemTool")
IFS = mod.InMemoryFileSystem

IMG = "/tmp/lisaem-profile-5MB-FreshInstallOf_PascalWorkshop1.0.dc42"
REF = sys.argv[1] if len(sys.argv) > 1 else None


def load(path):
    fs = IFS(path)
    mddf = fs._mddf_sector_number
    m = fs.read_sector(mddf)
    u16 = lambda o: struct.unpack_from(">H", m, o)[0]
    u32 = lambda o: struct.unpack_from(">I", m, o)[0]
    info = {
        "fs": fs,
        "mddf": mddf,
        "num_sectors": fs._num_sectors,
        "bitmap_addr": u32(0x88),
        "bitmap_size": u32(0x8C),
        "bitmap_pages": u16(0x92),
        "slist_addr": u32(0x94),
        "slist_packing": u16(0x98),
        "slist_blocks": u16(0x9A),
        "empty_file": u16(0x9E),
        "maxfiles": u16(0xA0),
        "filecount": u16(0xB0),
        "freecount": u32(0xBA),
    }
    bmp = b"".join(fs.read_sector(mddf + info["bitmap_addr"] + i) for i in range(info["bitmap_pages"]))
    info["bmp"] = bmp
    return info


def is_alloc(info, rel):
    return (info["bmp"][rel // 8] >> (rel % 8)) & 1 == 1


def file_pages(info, s):
    """All MDDF-relative pages (hint chain + data chain) referenced by sfile s."""
    e = info["fs"]._slist_entry(s)
    if e is None or e[0] == 0:
        return None
    hintaddr, fileaddr, fsize, ver = e
    mddf = info["mddf"]
    pages = []
    # hint chain via tag fwdlinks
    rel = hintaddr
    seen = set()
    while rel and rel not in seen:
        seen.add(rel)
        pages.append(rel)
        tag = info["fs"].read_tags_for_sector(mddf + rel)
        fwd = (tag[0x0E] << 16) | (tag[0x0F] << 8) | tag[0x10]
        if fwd == 0xFFFFFF:
            break
        rel = fwd
    # data chain
    rel = fileaddr
    seen = set()
    while rel and rel not in seen:
        seen.add(rel)
        pages.append(rel)
        tag = info["fs"].read_tags_for_sector(mddf + rel)
        fwd = (tag[0x0E] << 16) | (tag[0x0F] << 8) | tag[0x10]
        if fwd == 0xFFFFFF:
            break
        rel = fwd
    return pages


def sfile_name(info, s):
    e = info["fs"]._slist_entry(s)
    if e is None or e[0] == 0:
        return None
    mddf = info["mddf"]
    try:
        h = info["fs"].read_sector(mddf + e[0])
        n = h[0]
        return h[1:1+n].decode("mac-roman", errors="replace")
    except Exception:
        return "?"


def main():
    info = load(IMG)
    mddf = info["mddf"]
    nrel = info["num_sectors"] - mddf  # relative pages 0..nrel-1
    ba, bs, bp = info["bitmap_addr"], info["bitmap_size"], info["bitmap_pages"]
    print(f"Image: {IMG}")
    print(f"MDDF sector: {mddf}, total sectors: {info['num_sectors']}, relative pages: {nrel}")
    print(f"Bitmap: addr={ba} (abs {mddf+ba}), size={bs} bits, pages={bp} (covers {bp*4096} bits)")
    print(f"MDDF: filecount={info['filecount']} empty_file={info['empty_file']} maxfiles={info['maxfiles']} freecount={info['freecount']}")
    print()

    # 1+2: walk all files
    allocated_by_files = {}
    problems = []
    for s in range(1, info["empty_file"]):
        pages = file_pages(info, s)
        if pages is None:
            continue
        name = sfile_name(info, s)
        allocated_by_files[s] = (name, pages)
        bad = [p for p in pages if not is_alloc(info, p)]
        if bad:
            problems.append((s, name, bad))

    print(f"Files found: {len(allocated_by_files)}")
    print()

    # 3: Diff2.obj details
    for s in sorted(allocated_by_files):
        name, pages = allocated_by_files[s]
        if name == "Diff2.obj":
            e = info["fs"]._slist_entry(s)
            print(f"Diff2.obj: sfile={s} hintaddr(rel)={e[0]} fileaddr(rel)={e[1]} filesize={e[2]}")
            print(f"  hint pages (rel/abs): {[(p, mddf+p) for p in file_pages(info,s) if p==e[0]]}")
            print(f"  ALL its pages rel {min(pages)}..{max(pages)} -> abs {mddf+min(pages)}..{mddf+max(pages)}")
            for p in sorted(pages):
                tag = info["fs"].read_tags_for_sector(mddf + p)
                fid = struct.unpack_from(">H", tag, 4)[0]
                du = struct.unpack_from(">H", tag, 6)[0]
                bit = "ALLOC" if is_alloc(info, p) else "*** FREE ***"
                print(f"    rel {p:5d} (abs {mddf+p:5d}) tag fileid={fid:5d} dataused={du:3d}  bitmap: {bit}")
    print()

    # 2: problems
    if problems:
        print("PAGES NOT MARKED ALLOCATED (should be allocated):")
        for s, name, bad in problems:
            print(f"  sfile {s} '{name}': {[(p, mddf+p) for p in bad]}")
    else:
        print("OK: every hint/data page of every file is marked allocated in the bitmap.")
    print()

    # 4: allocated but unreferenced
    referenced = set()
    for s, (name, pages) in allocated_by_files.items():
        referenced.update(pages)
    unreferenced = [p for p in range(nrel) if is_alloc(info, p) and p not in referenced]
    # known system pages: MDDF (0), slist pages, bitmap pages
    system = {0}
    for i in range(info["slist_blocks"]):
        system.add(info["slist_addr"] + i)
    for i in range(bp):
        system.add(ba + i)
    unref_sys = sorted(p for p in unreferenced if p in system)
    unref_other = sorted(p for p in unreferenced if p not in system)
    print(f"Allocated-but-unreferenced pages: {len(unreferenced)} total")
    if unref_sys:
        print(f"  system pages (MDDF/slist/bitmap): {[(p, mddf+p) for p in unref_sys]}")
    if unref_other:
        # group into runs
        runs = []
        for p in unref_other:
            if runs and p == runs[-1][1] + 1:
                runs[-1][1] = p
            else:
                runs.append([p, p])
        print(f"  OTHER unreferenced allocated pages: {len(unref_other)}")
        for a, b in runs:
            print(f"    rel {a}..{b} (abs {mddf+a}..{mddf+b}, {b-a+1} pages)")
    print()

    # 5: freecount check
    limit = min(bs, nrel)
    free_bits = sum(1 for p in range(limit) if not is_alloc(info, p))
    print(f"Free bits in bitmap (over {limit} pages): {free_bits}; MDDF freecount: {info['freecount']}"
          + ("  -> MATCH" if free_bits == info["freecount"] else "  -> MISMATCH!"))
    print()

    # 6: diff against reference
    if REF:
        print(f"Reference: {REF}")
        ref = load(REF)
        ba2, bs2, bp2 = ref["bitmap_addr"], ref["bitmap_size"], ref["bitmap_pages"]
        print(f"Ref bitmap: addr={ba2} (abs {ref['mddf']+ba2}), size={bs2}, pages={bp2}; ref freecount={ref['freecount']}")
        n = min(len(info["bmp"]), len(ref["bmp"]))
        diffs = []
        for p in range(min(limit, min(bs2, ref["num_sectors"] - ref["mddf"]))):
            b1 = (info["bmp"][p // 8] >> (p % 8)) & 1
            b2 = (ref["bmp"][p // 8] >> (p % 8)) & 1
            if b1 != b2:
                diffs.append((p, b1, b2))
        if diffs:
            runs = []
            for p, b1, b2 in diffs:
                if runs and p == runs[-1][1][0] + 1:
                    runs[-1][1] = (p, b1, b2)
                else:
                    runs.append([(p, b1, b2), (p, b1, b2)])
            print(f"Bitmap bits changed vs reference: {len(diffs)}")
            for first, last in runs:
                tag = "free->alloc" if last[1] else "alloc->free"
                if first[0] == last[0]:
                    print(f"    rel {first[0]} (abs {mddf+first[0]}): {tag}")
                else:
                    print(f"    rel {first[0]}..{last[0]} (abs {mddf+first[0]}..{mddf+last[0]}, {last[0]-first[0]+1} pages): {tag}")
        else:
            print("Bitmap identical to reference.")
        # also sector-level diff of the whole image
        import hashlib
        def all_sectors(fs, mddf):
            out = []
            for i in range(fs._num_sectors):
                out.append(fs.read_sector(i))
                out.append(fs.read_tags_for_sector(i))
            return b"".join(out)
        h1 = hashlib.md5(all_sectors(info["fs"], mddf)).hexdigest()
        h2 = hashlib.md5(all_sectors(ref["fs"], ref["mddf"])).hexdigest()
        print(f"Whole-image (data+tags) md5: current={h1} ref={h2}")
        changed = []
        for i in range(info["num_sectors"]):
            d1 = info["fs"].read_sector(i)
            d2 = ref["fs"].read_sector(i)
            t1 = info["fs"].read_tags_for_sector(i)
            t2 = ref["fs"].read_tags_for_sector(i)
            if d1 != d2 or t1 != t2:
                changed.append((i, d1 != d2, t1 != t2))
        print(f"Sectors changed vs reference: {len(changed)}")
        for i, dd, tt in changed:
            print(f"    sector {i}: data={'CHANGED' if dd else 'same'} tag={'CHANGED' if tt else 'same'}")


if __name__ == "__main__":
    main()
