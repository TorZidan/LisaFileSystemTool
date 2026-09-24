#!/usr/bin/env python3
"""Test harness for the delete command in LisaFileSystemToolPerFile.py.

Run:  python3 test_deletefile.py

Copies two real disk images to /tmp/deltest/ and exercises delete on the
copies (the originals are never touched):
  * a b-tree volume (fs_version 17, LOS 3.1):
      LisaSourceCompilation/LOS_Compilation_Base.image
  * a flat-catalog volume (fs_version 14, LOS 1.0):
      /tmp/
          50MB_FreshInstallOfLOS1.0.image

Checks after every operation:
  * whole-volume tag checksums (XOR of data + tag-except-byte-11 must be 0),
  * MDDF freecount vs actual free-bit count (delta must be unchanged),
  * b-tree structural health (key ranges, interior/leaf levels, leaf chain),
  * sentry emptied, catalog entry gone, MDDF counters consistent.
"""
import contextlib
import io
import os
import shutil
import struct
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from LisaFileSystemTool import InMemoryFileSystem
from LisaFileSystemToolPerFile import FileSystemWithAddFile, BTREE_BAD, BTREE_LEAF

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(REPO_ROOT, "LisaFileSystemToolPerFile.py")
WORK = "/tmp/deltest"
SRC_B = os.path.join(REPO_ROOT, "LisaSourceCompilation", "LOS_Compilation_Base.image")
SRC_F = "/tmp/50MB_FreshInstallOfLOS1.0.image"

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
        return FileSystemWithAddFile(image)


def tag_checksums_ok(image) -> int:
    """Number of sectors whose stored tag checksum does not match."""
    data = open(image, "rb").read()
    assert len(data) % 532 == 0, "expected a raw interleaved image"
    num = len(data) // 532
    bad = 0
    d = (0, 4, 8, 12, 0, 4, 8, -4, 0, 4, -8, -4, 0, -12, -8, -4)
    for sec in range(num):
        pos = (sec + d[sec & 15]) * 532
        tag = data[pos : pos + 20]
        sector_data = data[pos + 20 : pos + 532]
        if not (struct.unpack(">H", tag[6:8])[0] & 0x8000):
            continue
        c = 0
        for b in sector_data:
            c ^= b
        for b in tag:
            c ^= b
        if c != 0:
            bad += 1
    return bad


def freecount_delta(image) -> int:
    """MDDF freecount minus the actual number of free bits in the bitmap."""
    fs = quiet_fs(image)
    m = fs.read_sector(fs._mddf_sector_number)
    freecount = struct.unpack(">I", m[0xBA:0xBE])[0]
    free = len(fs._bitmap_free_pages())
    return freecount - free


def file_names_btree(fs):
    return [
        rec[3:35].split(b"\x00", 1)[0].decode("mac-roman", "replace")
        for _, _, rec in fs._bt_scan_entries()
        if rec[36] == 3
    ]


def file_names_flat(fs):
    rc_sfile = fs._find_rootcatalog_sfile()
    rc = fs._slist_entry(rc_sfile)
    chain = fs._data_page_chain(rc[1])
    cat_data = b"".join(fs.read_sector(fs._mddf_sector_number + p) for p in chain)
    rme = fs._mddf_u16(0xC0)
    names = []
    for i in range(rme):
        rec = cat_data[i * 54 : i * 54 + 54]
        nl, ce = rec[0], rec[34]
        if ce == 3 and nl:
            names.append(rec[1 : 1 + nl].decode("mac-roman", "replace"))
    return names


def find_sfile(fs, name):
    name_u = name.upper()
    if fs.is_flat_catalog_volume():
        from LisaFileSystemTool import flat_catalog_hash

        rc_sfile = fs._find_rootcatalog_sfile()
        rc = fs._slist_entry(rc_sfile)
        chain = fs._data_page_chain(rc[1])
        cat_data = b"".join(fs.read_sector(fs._mddf_sector_number + p) for p in chain)
        rme = fs._mddf_u16(0xC0)
        start = flat_catalog_hash(name, rme)
        for i in range(rme):
            idx = (start + i) % rme
            rec = cat_data[idx * 54 : idx * 54 + 54]
            nl, ce = rec[0], rec[34]
            if ce == 0 and nl == 0:
                return None
            if ce == 7:
                continue
            if nl and rec[1 : 1 + nl].decode("mac-roman", "replace").upper() == name_u:
                return struct.unpack(">H", rec[36:38])[0] if ce == 3 else None
        return None
    else:
        for node, idx, rec in fs._bt_scan_entries():
            if rec[36] != 3:
                continue
            nm = rec[3:35].split(b"\x00", 1)[0].decode("mac-roman", "replace")
            if nm.upper() == name_u:
                return struct.unpack(">H", rec[38:40])[0]
        return None


def sentry(fs, sfile):
    m = fs.read_sector(fs._mddf_sector_number)
    slist_addr = struct.unpack(">I", m[0x94:0x98])[0]
    packing = struct.unpack(">H", m[0x98:0x9A])[0]
    spage = sfile // packing
    soffs = (sfile % packing) * 14
    sl = fs.read_sector(fs._mddf_sector_number + slist_addr + spage)
    hintaddr, fileaddr, filesize, ver = struct.unpack(">II IH", sl[soffs : soffs + 14])
    return hintaddr, fileaddr, filesize, ver


def tree_healthy(fs):
    """Structural health of a b-tree catalog: key ordering, key ranges
    (child i holds keys in [K_i, K_{i+1})), uniform interior record size,
    uniform leaf depth, and the leaf prior/next chain. Returns [problems]."""
    root = fs._mddf_u32(0x12E)
    if root == BTREE_BAD:
        return []
    bad = []
    leaf_levels = set()

    def walk(page, lo, hi, lvl, seen):
        if page in seen:
            bad.append(f"cycle at {page}")
            return
        seen.add(page)
        node = fs._bt_read_node(page)
        if node.kind != BTREE_LEAF:
            prev = None
            for i, rec in enumerate(node.records):
                if len(rec) != 40:
                    bad.append(f"node {page} rec {i} is not 40 bytes (MIXED)")
                    continue
                k = rec[4:40]
                if prev is not None and fs._bt_key_cmp(k, prev) <= 0:
                    bad.append(f"node {page}: unsorted separators")
                child = struct.unpack(">I", rec[0:4])[0]
                walk(child,
                     lo if i == 0 else rec[4:40],
                     hi if i == node.nkeys - 1 else node.records[i + 1][4:40],
                     lvl + 1, seen)
                prev = k
        else:
            leaf_levels.add(lvl)
            prev = None
            for i, rec in enumerate(node.records):
                k = rec[0:36]
                if lo is not None and fs._bt_key_cmp(k, lo) < 0:
                    bad.append(f"leaf {page} rec {i} below lower bound")
                if hi is not None and fs._bt_key_cmp(k, hi) >= 0:
                    bad.append(f"leaf {page} rec {i} at/above upper bound")
                if prev is not None and fs._bt_key_cmp(k, prev) <= 0:
                    bad.append(f"leaf {page}: unsorted records")
                prev = k

    walk(root, None, None, 1, set())
    if len(leaf_levels) > 1:
        bad.append(f"leaves at multiple depths: {sorted(leaf_levels)}")
    # leaf chain
    leaves = {}
    stack = [root]
    while stack:
        p = stack.pop()
        node = fs._bt_read_node(p)
        if node.kind != BTREE_LEAF:
            for rec in node.records:
                if len(rec) == 40:
                    stack.append(struct.unpack(">I", rec[0:4])[0])
        else:
            leaves[p] = (node.prior, node.next)
    leftmost = [p for p, (pr, nx) in leaves.items() if pr == BTREE_BAD]
    if len(leftmost) != 1:
        bad.append(f"leftmost-leaf count = {len(leftmost)}")
    else:
        p = leftmost[0]
        seen = set()
        while p != BTREE_BAD:
            if p in seen or p not in leaves:
                bad.append(f"chain broken/cycle at {p}")
                break
            seen.add(p)
            p = leaves[p][1]
        if set(leaves) - seen:
            bad.append(f"unreached leaves: {sorted(set(leaves) - seen)[:4]}")
    for p, (pr, nx) in leaves.items():
        if pr != BTREE_BAD and (pr not in leaves or leaves[pr][1] != p):
            bad.append(f"chain mismatch: {p}.prior={pr}")
        if nx != BTREE_BAD and (nx not in leaves or leaves[nx][0] != p):
            bad.append(f"chain mismatch: {p}.next={nx}")
    return bad


def mddf_counters(fs):
    m = fs.read_sector(fs._mddf_sector_number)
    return {
        "filecount": struct.unpack(">H", m[0xB0:0xB2])[0],
        "freecount": struct.unpack(">I", m[0xBA:0xBE])[0],
        "root_page": struct.unpack(">I", m[0x12E:0x132])[0],
        "tree_depth": struct.unpack(">H", m[0x132:0x134])[0],
    }


def run_delete(image, name):
    r = subprocess.run([sys.executable, TOOL, "delete", image, name],
                       capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


def run_add(image, host, name):
    r = subprocess.run([sys.executable, TOOL, "add", image, host, name],
                       capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


def check_volume(image, base_tags, base_delta, is_btree, label):
    """Whole-volume invariants after an operation."""
    t = tag_checksums_ok(image)
    report(t == base_tags, f"{label}: whole-volume tag checksums ({t} bad, was {base_tags})")
    d = freecount_delta(image)
    report(d == base_delta, f"{label}: freecount delta unchanged ({d}, was {base_delta})")
    if is_btree:
        fs = quiet_fs(image)
        probs = tree_healthy(fs)
        report(not probs, f"{label}: b-tree structure healthy"
             + ("" if not probs else f" ({'; '.join(probs[:4])})"))


def main():
    os.makedirs(WORK, exist_ok=True)
    btree_img = os.path.join(WORK, "btree.image")
    flat_img = os.path.join(WORK, "flat.image")
    shutil.copy(SRC_B, btree_img)
    shutil.copy(SRC_F, flat_img)

    print("== baseline ==")
    b_tags = tag_checksums_ok(btree_img)
    f_tags = tag_checksums_ok(flat_img)
    b_delta = freecount_delta(btree_img)
    f_delta = freecount_delta(flat_img)
    fs = quiet_fs(btree_img)
    print(f"b-tree: bad tags={b_tags}, freecount delta={b_delta}, counters={mddf_counters(fs)}")
    fs2 = quiet_fs(flat_img)
    print(f"flat:   bad tags={f_tags}, freecount delta={f_delta}")

    all_names = file_names_btree(fs)
    text_names = [n for n in all_names if n.upper().endswith(".TEXT")]
    bin_names = [n for n in all_names if not n.upper().endswith(".TEXT")]
    assert text_names and bin_names, "image has no .TEXT or non-.TEXT files to test"
    victim_text = text_names[0]
    victim_bin = bin_names[0]
    vs_text = find_sfile(fs, victim_text)
    vs_bin = find_sfile(fs, victim_bin)
    size_text = sentry(fs, vs_text)[2]
    size_bin = sentry(fs, vs_bin)[2]
    ver_text_before = sentry(fs, vs_text)[3]
    fc_before = mddf_counters(fs)["filecount"]
    print(f"b-tree victims: {victim_text!r} sfile={vs_text} size={size_text}, "
          f"{victim_bin!r} sfile={vs_bin} size={size_bin}")

    # ===================================================================
    print("\n== T1: b-tree delete of a .TEXT file ==")
    rc, out, err = run_delete(btree_img, victim_text)
    print(out, err)
    report(rc == 0, f"exit code 0 (got {rc})")
    check_volume(btree_img, b_tags, b_delta, True, "T1")
    fs = quiet_fs(btree_img)
    report(find_sfile(fs, victim_text) is None, "T1: catalog entry gone")
    h, fa, sz, ver = sentry(fs, vs_text)
    report((h, fa, sz) == (0, 0, 0), f"T1: sentry emptied (hintaddr={h}, fileaddr={fa}, filesize={sz})")
    report(ver == (ver_text_before + 1) & 0xFFFF, f"T1: sentry version incremented ({ver_text_before} -> {ver})")
    report(mddf_counters(fs)["filecount"] == fc_before - 1, "T1: MDDF filecount decremented")

    # ===================================================================
    print("\n== T2: b-tree delete of a (multi-page) binary file ==")
    pages_before = sentry(fs, vs_bin)[1]  # fileaddr for the page count below
    chain_before = fs._data_page_chain(pages_before)
    rc, out, err = run_delete(btree_img, victim_bin)
    print(out, err)
    report(rc == 0, f"exit code 0 (got {rc})")
    check_volume(btree_img, b_tags, b_delta, True, "T2")
    fs = quiet_fs(btree_img)
    report(find_sfile(fs, victim_bin) is None, "T2: catalog entry gone")
    h, fa, sz, _ = sentry(fs, vs_bin)
    report((h, fa, sz) == (0, 0, 0), "T2: sentry emptied")
    # all former data pages must be free in the bitmap
    free_now = set(fs._bitmap_free_pages())
    report(all(p in free_now for p in chain_before),
           f"T2: all {len(chain_before)} former data pages are free in the bitmap")

    # ===================================================================
    print("\n== T3: b-tree non-existent name -> exit 3, image unchanged ==")
    before = open(btree_img, "rb").read()
    rc, out, err = run_delete(btree_img, "NO/SUCH.FILE.TEXT")
    print(out, err)
    report(rc == 3, f"exit code 3 (got {rc})")
    report(open(btree_img, "rb").read() == before, "image byte-identical")

    # ===================================================================
    print("\n== T4: b-tree similar-but-nonexistent name -> exit 3 ==")
    name_set = {n.upper() for n in file_names_btree(quiet_fs(btree_img))}
    t4 = victim_text
    while t4.upper() in name_set:
        t4 += "9"
    rc, out, err = run_delete(btree_img, t4)
    print(out, err)
    report(rc == 3, f"exit code 3 (got {rc})")
    report(open(btree_img, "rb").read() == before, "image byte-identical")

    # ===================================================================
    print("\n== T5: b-tree directory-style name -> exit 3 (no such file record) ==")
    t5 = victim_text.split("/")[0]  # a top-level directory name
    fsx = quiet_fs(btree_img)
    assert find_sfile(fsx, t5) is None, f"{t5!r} unexpectedly resolves to a file"
    rc, out, err = run_delete(btree_img, t5)
    print(out, err)
    report(rc == 3, f"exit code 3 (got {rc})")
    check_volume(btree_img, b_tags, b_delta, True, "T5")

    # ===================================================================
    print("\n== T6: case-insensitive delete ==")
    rc, out, err = run_delete(btree_img, victim_text.lower())
    print(out, err)
    report(rc == 3, f"already deleted: lowercase name -> exit 3 (got {rc})")
    victim2 = text_names[1] if len(text_names) > 1 else bin_names[1]
    rc, out, err = run_delete(btree_img, victim2.lower())
    print(out, err)
    report(rc == 0, f"lowercase name of a live file -> exit 0 (got {rc})")
    check_volume(btree_img, b_tags, b_delta, True, "T6")

    # ===================================================================
    print("\n== T7: add + delete round trip (incl. zero-byte file) ==")
    with open(f"{WORK}/empty.bin", "wb"):
        pass
    rc, out, err = run_add(btree_img, f"{WORK}/empty.bin", "DELETEST/EMPTY.BIN")
    report(rc == 0, f"add empty file exit 0 (got {rc})")
    rc, out, err = run_delete(btree_img, "DELETEST/EMPTY.BIN")
    print(out, err)
    report(rc == 0, f"delete zero-byte file exit 0 (got {rc})")
    check_volume(btree_img, b_tags, b_delta, True, "T7")
    fs = quiet_fs(btree_img)
    report(find_sfile(fs, "DELETEST/EMPTY.BIN") is None, "T7: round-trip file gone")

    # ===================================================================
    print("\n== T8: re-add after delete reuses the name cleanly ==")
    import random
    random.seed(7)
    with open(f"{WORK}/again.bin", "wb") as f:
        f.write(bytes(random.randrange(256) for _ in range(5000)))
    rc, out, err = run_add(btree_img, f"{WORK}/again.bin", "DELETEST/AGAIN.BIN")
    report(rc == 0, f"add exit 0 (got {rc})")
    rc, out, err = run_delete(btree_img, "DELETEST/AGAIN.BIN")
    report(rc == 0, f"delete exit 0 (got {rc})")
    rc, out, err = run_add(btree_img, f"{WORK}/again.bin", "DELETEST/AGAIN.BIN")
    print(out, err)
    report(rc == 0, f"re-add same name exit 0 (got {rc})")
    check_volume(btree_img, b_tags, b_delta, True, "T8")
    fs = quiet_fs(btree_img)
    s = find_sfile(fs, "DELETEST/AGAIN.BIN")
    report(s is not None and sentry(fs, s)[2] == 5000, "T8: re-added file present with correct size")

    # ===================================================================
    print("\n== T9: bulk delete (20 files) exercises many rebalances ==")
    fs = quiet_fs(btree_img)
    remaining = [n for n in file_names_btree(fs)
                 if n.upper() not in ("DELETEST/AGAIN.BIN",)][:20]
    ok_all = True
    for n in remaining:
        rc, out, err = run_delete(btree_img, n)
        if rc != 0:
            print(out, err)
            ok_all = False
            break
    report(ok_all, f"all {len(remaining)} bulk deletes exited 0")
    check_volume(btree_img, b_tags, b_delta, True, "T9")

    # ===================================================================
    # flat-catalog volume
    fs = quiet_fs(flat_img)
    fnames = file_names_flat(fs)
    print(f"\n== flat volume: {len(fnames)} files, sample: {fnames[:12]}")

    # ===================================================================
    print("\n== F1: flat delete of a .TEXT file ==")
    victim = next((n for n in fnames if n.upper().endswith(".TEXT")), fnames[0])
    vs = find_sfile(fs, victim)
    ver_before = sentry(fs, vs)[3]
    rc, out, err = run_delete(flat_img, victim)
    print(out, err)
    report(rc == 0, f"exit code 0 (got {rc})")
    t = tag_checksums_ok(flat_img)
    report(t == f_tags, f"F1: whole-volume tag checksums ({t} bad, was {f_tags})")
    d = freecount_delta(flat_img)
    report(d == f_delta, f"F1: freecount delta unchanged ({d}, was {f_delta})")
    fs = quiet_fs(flat_img)
    report(find_sfile(fs, victim) is None, "F1: centry gone")
    h, fa, sz, ver = sentry(fs, vs)
    report((h, fa, sz) == (0, 0, 0), "F1: sentry emptied")
    report(ver == (ver_before + 1) & 0xFFFF, f"F1: sentry version incremented ({ver_before} -> {ver})")

    # ===================================================================
    print("\n== F2: flat directory entry (cetype 4) -> exit 1, unchanged ==")
    before = open(flat_img, "rb").read()
    # find a directory entry dynamically
    rc_sfile = fs._find_rootcatalog_sfile()
    rc = fs._slist_entry(rc_sfile)
    chain = fs._data_page_chain(rc[1])
    cat_data = b"".join(fs.read_sector(fs._mddf_sector_number + p) for p in chain)
    rme = fs._mddf_u16(0xC0)
    dir_name = None
    for i in range(rme):
        rec = cat_data[i * 54 : i * 54 + 54]
        nl, ce = rec[0], rec[34]
        if ce == 4 and nl:
            dir_name = rec[1 : 1 + nl].decode("mac-roman", "replace")
            break
    assert dir_name is not None, "flat image has no directory centry to test"
    rc, out, err = run_delete(flat_img, dir_name)
    print(out, err)
    report(rc == 1, f"exit code 1 (got {rc})")
    report(open(flat_img, "rb").read() == before, "image byte-identical")

    # ===================================================================
    print("\n== F3: flat non-existent -> exit 3, unchanged ==")
    rc, out, err = run_delete(flat_img, "NO/SUCH.FILE")
    print(out, err)
    report(rc == 3, f"exit code 3 (got {rc})")
    report(open(flat_img, "rb").read() == before, "image byte-identical")

    # ===================================================================
    print(f"\n===== RESULTS: {PASS} passed, {FAIL} failed =====")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
