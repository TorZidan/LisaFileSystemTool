#!/usr/bin/env python3
"""Regression test for the delete command on two real flat-catalog hard disk
volumes (DC42, fs_version 15 / LOS 2.0, 9728 sectors, 20-byte tags):
  * LisaSourceCompilation/LOS_Compilation_Base.image
  * LisaSourceCompilation/lisaem-profile-5MB-FreshInstallOf_LOS2.0.dc42

Run:  python3 test_delete_real_volumes.py

Copies both images to /tmp/deltest_volumes/ and, on each copy, deletes the
biggest file (a multi-run file with a multi-page file map) plus two small
files.  After every delete it checks:
  * the MDDF freecount increments by exactly the file's sectors
    (file map pages + hint pages),
  * the allocation bitmap marks exactly those sectors free and no free bit is
    lost,
  * the freecount-vs-bitmap delta is preserved,
  * the MDDF filecount decrements.
The DC42 header data/tag checksums are verified by the tool itself on every
load and save (it prints "OK" or updates them).
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
SRC = {
    "compilation_base": os.path.join(
        REPO_ROOT, "LisaSourceCompilation", "LOS_Compilation_Base.image"),
    "los20_fresh": os.path.join(
        REPO_ROOT, "LisaSourceCompilation",
        "lisaem-profile-5MB-FreshInstallOf_LOS2.0.dc42"),
}
WORK = "/tmp/deltest_volumes"

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
    }


def free_pages(fs):
    return set(fs._bitmap_free_pages())


def flat_files(fs):
    cat_data, _chain, rme, _sz = fs._flat_read_rootcatalog()
    out = []
    for i in range(rme):
        rec = cat_data[i * 54 : i * 54 + 54]
        nl, ce = rec[0], rec[34]
        if ce == 3 and nl:
            name = rec[1 : 1 + nl].decode("mac-roman", "replace")
            sfile = struct.unpack(">H", rec[36:38])[0]
            out.append((name, sfile))
    return out


def file_sectors(fs, sfile):
    hintaddr, fileaddr, filesize, ver = fs._slist_entry(sfile)
    hint = fs._hint_page_chain(hintaddr) if hintaddr else []
    smallmap_off = fs._mddf_u16(0x114)
    map_off = fs._mddf_u16(0xAC)
    is_btree = not fs.is_flat_catalog_volume()
    map = fs._file_map_all_pages(hint, is_btree, smallmap_off, map_off)
    chain = fs._data_page_chain(fileaddr) if fileaddr else []
    return hint, fileaddr, filesize, map, chain


def main():
    missing = [p for p in SRC.values() if not os.path.isfile(p)]
    if missing:
        print(f"SKIP: fixture image(s) not found: {missing}")
        return 0
    os.makedirs(WORK, exist_ok=True)
    for key, src in SRC.items():
        img = os.path.join(WORK, key + ".img")
        shutil.copy(src, img)
        print(f"===== {os.path.basename(src)} -> {img} =====")
        fs = quiet_fs(img)
        assert fs.is_flat_catalog_volume(), "expected a flat-catalog volume"
        files = flat_files(fs)
        # pick: the biggest file (multi-page map) + two small ones
        sized = []
        for name, sfile in files:
            if name.upper() == "ROOTCATALOG":
                continue
            h, fa, sz, mp, ch = file_sectors(fs, sfile)
            sized.append((sz, name, sfile, len(h), len(mp) if mp else -1, len(ch)))
        sized.sort(reverse=True)
        picks = [sized[0]] + [s for s in sized if s[4] <= 1][:2]
        st0 = mddf_state(fs)
        fp0 = free_pages(fs)
        delta0 = st0["freecount"] - len(fp0)
        print(f"  baseline: filecount={st0['filecount']} freecount={st0['freecount']} "
              f"bitmap free={len(fp0)} (delta {delta0})")
        for sz, name, sfile, nh, nmap, nch in picks:
            fs = quiet_fs(img)
            hint, fa, filesize, map, chain = file_sectors(fs, sfile)
            if map is not None:
                data = map
            else:
                data = chain
            freed = set(data) | set(hint)
            st_b = mddf_state(fs)
            fp_b = free_pages(fs)
            print(f"  --- delete '{name}' (sfile {sfile}, {filesize} B; "
                  f"map={nmap} chain={nch} hint={nh}) ---")
            r = subprocess.run([sys.executable, TOOL, "delete", img, name],
                               capture_output=True, text=True)
            for line in r.stdout.splitlines():
                if line.startswith(("Deleted", "NOTE", "ERROR")):
                    print(f"    {line}")
            report(r.returncode == 0, f"exit 0 (got {r.returncode})")
            fs = quiet_fs(img)
            st_a = mddf_state(fs)
            fp_a = free_pages(fs)
            d = st_a["freecount"] - st_b["freecount"]
            report(d == len(freed), f"freecount +{d} == {len(freed)} sectors")
            report(fp_a - fp_b == freed,
                   "bitmap: exactly the deleted sectors became free")
            report(fp_b <= fp_a, "bitmap: no free bit lost")
            report(st_a["freecount"] - len(fp_a) == delta0,
                   f"freecount/bitmap delta preserved ({delta0})")
            report(st_a["filecount"] == st_b["filecount"] - 1, "filecount -1")
        fs = quiet_fs(img)
        st = mddf_state(fs)
        fp = free_pages(fs)
        report(st["freecount"] - len(fp) == delta0,
               f"final: freecount {st['freecount']} vs bitmap {len(fp)} (delta {delta0})")
        print()
    print(f"===== RESULTS: {PASS} passed, {FAIL} failed =====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
