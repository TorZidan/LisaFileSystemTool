#!/usr/bin/env python3
"""Test harness for the delete command on the flat-catalog floppy image
LisaSourceCompilation/test.dc42 (DC42, fs_version 15 / LOS 2.0, 800 sectors,
12-byte tags, hintsize 1, MDDF sector 28).

Run:  python3 test_delete_testdc42.py

Copies the image to /tmp/deltest_dc42/ and deletes every file except
rootcatalog (which has no centry on this volume — the tool's 'list' command
synthesizes it).  After every delete it checks:
  * the MDDF freecount (offset 0xBA) increments by exactly the number of
    sectors the file occupied (file map pages + hint pages),
  * the allocation bitmap marks exactly those sectors free and no other bit
    changes,
  * the MDDF filecount decrements and the sentry is emptied.
Finally it checks:
  * freecount == bitmap free-bit count == baseline + total freed,
  * the catalog holds only tombstones (KILL_ENTRY bookkeeping: slots whose
    entry straddles a page boundary become 'removed' (7), the others
    'emptyentry' (0); one 'removed' tombstone pre-exists in the image).

This image also exercises the file-map-based page release: SYS1LIB.OBJ's tag
chain is broken (page 763's fwd link is END instead of 25), so only 282 of
its 323 data pages are chain-reachable; the file map lists all 323, and
delete must free all of them (as the OS's FMAP_MGR FMRELEASE does), not just
the 282 the chain reaches.
"""
import contextlib
import io
import os
import shutil
import struct
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
from LisaFileSystemToolPerFile import FileSystemWithPerFileCommands  # noqa: E402

TOOL = os.path.join(REPO_ROOT, "LisaFileSystemToolPerFile.py")
SRC = os.path.join(REPO_ROOT, "LisaSourceCompilation", "test.dc42")
WORK = "/tmp/deltest_dc42"

PASS = 0
FAIL = 0


def report(ok, msg):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {msg}")
    else:
        FAIL += 1
        print(f"  [FAIL] {msg}")


def quiet_fs(image):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        return FileSystemWithPerFileCommands(image)


def mddf_state(fs):
    m = fs.read_sector(fs._mddf_sector_number)
    return {
        "filecount": struct.unpack(">H", m[0xB0:0xB2])[0],
        "freecount": struct.unpack(">I", m[0xBA:0xBE])[0],
        "empty_file": struct.unpack(">H", m[0x9E:0xA0])[0],
        "volsize": struct.unpack(">I", m[0x8C:0x90])[0],
    }


def free_pages(fs):
    return set(fs._bitmap_free_pages())


def list_flat_files(fs):
    """[(name, sfile, slot), ...] for every file centry (cetype 3)."""
    cat_data, _chain, rme, _sz = fs._flat_read_rootcatalog()
    out = []
    for i in range(rme):
        rec = cat_data[i * 54 : i * 54 + 54]
        nl, ce = rec[0], rec[34]
        if ce == 3 and nl:
            name = rec[1 : 1 + nl].decode("mac-roman", "replace")
            sfile = struct.unpack(">H", rec[36:38])[0]
            out.append((name, sfile, i))
    return out


def file_sectors(fs, sfile):
    """The sectors the file occupies, the OS way: the file map's pages
    (all map pages), cross-checked with the sentry and the tagged sectors."""
    mddf = fs._mddf_sector_number
    hintaddr, fileaddr, filesize, ver = fs._slist_entry(sfile)
    hint = fs._hint_page_chain(hintaddr) if hintaddr else []
    smallmap_off = fs._mddf_u16(0x114)
    map_off = fs._mddf_u16(0xAC)
    is_btree = not fs.is_flat_catalog_volume()
    map = fs._file_map_all_pages(hint, is_btree, smallmap_off, map_off)
    chain = fs._data_page_chain(fileaddr) if fileaddr else []
    tagged = set()
    for sec in range(mddf, fs._num_sectors):
        tag = fs.read_tags_for_sector(sec)
        if struct.unpack(">H", tag[4:6])[0] == sfile:
            tagged.add(sec - mddf)
    return {
        "hint": hint,
        "map": set(map) if map is not None else None,
        "chain": set(chain),
        "tagged": tagged,
        "filesize": filesize,
        "expected_pages": (filesize + 511) // 512,
    }


def main():
    if not os.path.isfile(SRC):
        print(f"SKIP: fixture image not found: {SRC}")
        return 0
    os.makedirs(WORK, exist_ok=True)
    image = os.path.join(WORK, "test.dc42")
    shutil.copy(SRC, image)

    fs = quiet_fs(image)
    assert fs.is_flat_catalog_volume(), "expected a flat-catalog volume"
    files = list_flat_files(fs)
    st = mddf_state(fs)
    fp = free_pages(fs)
    print(f"Volume: {SRC}")
    print(f"MDDF baseline: filecount={st['filecount']} freecount={st['freecount']} "
          f"empty_file={st['empty_file']} volsize={st['volsize']}; "
          f"bitmap free bits = {len(fp)} (delta {st['freecount'] - len(fp)})")
    print(f"Files on the volume: {[n for n, _, _ in files]}\n")

    baseline_freecount = st["freecount"]
    victims = [(n, s, sl) for (n, s, sl) in files if n.upper() != "ROOTCATALOG"]
    victims.sort(key=lambda t: t[1])  # sfile order
    total_expected_freed = 0

    for name, sfile, slot in victims:
        fs = quiet_fs(image)
        info = file_sectors(fs, sfile)
        # the file's sectors: the map (OS source of truth); on a healthy
        # volume map == chain == tagged
        if info["map"] is not None:
            data = info["map"]
        else:
            data = info["chain"]
        freed = data | set(info["hint"])
        total_expected_freed += len(freed)

        st_before = mddf_state(fs)
        fp_before = free_pages(fs)

        print(f"--- delete '{name}' (sfile {sfile}, slot {slot}, "
              f"{info['filesize']} bytes) ---")
        print(f"  before: freecount={st_before['freecount']}, "
              f"bitmap free bits={len(fp_before)}")
        agree = (info["map"] == info["chain"] == info["tagged"])
        print(f"  sectors: map={len(info['map']) if info['map'] is not None else '?'} "
              f"chain={len(info['chain'])} tagged={len(info['tagged'])} "
              f"hint={len(info['hint'])} "
              f"({'all agree' if agree else 'DISAGREE'})")
        print(f"  expected freed sectors: {len(freed)} "
              f"({len(data)} data + {len(info['hint'])} hint)")

        r = subprocess.run([sys.executable, TOOL, "delete", image, name],
                           capture_output=True, text=True)
        sys.stdout.write(r.stdout)
        if r.stderr:
            sys.stdout.write(r.stderr)
        report(r.returncode == 0, f"delete exit code 0 (got {r.returncode})")

        fs = quiet_fs(image)
        st_after = mddf_state(fs)
        fp_after = free_pages(fs)
        print(f"  after:  freecount={st_after['freecount']}, "
              f"bitmap free bits={len(fp_after)}")

        d_fc = st_after["freecount"] - st_before["freecount"]
        report(d_fc == len(freed),
               f"MDDF freecount increment {d_fc} == sectors deleted {len(freed)}")

        newly_free = fp_after - fp_before
        report(newly_free == freed,
               f"bitmap: exactly the {len(freed)} deleted sectors became free "
               + ("" if newly_free == freed else
                  f"(diff: missing={sorted(freed - newly_free)[:8]} "
                  f"extra={sorted(newly_free - freed)[:8]})"))
        report(fp_before <= fp_after,
               "bitmap: no previously-free sector became allocated")
        report(st_after["filecount"] == st_before["filecount"] - 1,
               f"MDDF filecount decremented ({st_before['filecount']} -> {st_after['filecount']})")
        h2, fa2, sz2, ver2 = fs._slist_entry(sfile)
        report((h2, fa2, sz2) == (0, 0, 0),
               "sentry emptied (hintaddr/fileaddr/filesize = 0)")
        # the file's tags must no longer claim any sector as allocated
        still_alloc = [p for p in freed if p not in fp_after]
        report(not still_alloc,
               f"all {len(freed)} former sectors of the file are free in the bitmap")
        print()

    # ---- final consistency ----
    fs = quiet_fs(image)
    st = mddf_state(fs)
    fp = free_pages(fs)
    print("=== final state ===")
    print(f"MDDF: filecount={st['filecount']} freecount={st['freecount']} "
          f"empty_file={st['empty_file']} volsize={st['volsize']}")
    print(f"bitmap free bits = {len(fp)}")
    report(st["freecount"] == len(fp),
           f"final MDDF freecount ({st['freecount']}) == bitmap free-bit count ({len(fp)})")
    report(st["freecount"] == baseline_freecount + total_expected_freed,
           f"final freecount == baseline {baseline_freecount} + total freed "
           f"{total_expected_freed} ({baseline_freecount + total_expected_freed})")
    remaining = list_flat_files(fs)
    report(remaining == [],
           f"no user files remain in the catalog (found {remaining})")
    cat_data, _chain, rme, _sz = fs._flat_read_rootcatalog()
    tomb = sum(1 for i in range(rme) if cat_data[i * 54 + 34] == 7)
    live = sum(1 for i in range(rme)
               if cat_data[i * 54] and cat_data[i * 54 + 34] == 3)
    report(live == 0 and tomb == 4,
           f"catalog holds only tombstones ({tomb} removed incl. 1 pre-existing, "
           f"{live} live)")

    print(f"\n===== RESULTS: {PASS} passed, {FAIL} failed =====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
