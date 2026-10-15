#!/usr/bin/env python3
"""Test harness for the replace command in LisaFileSystemToolPerFile.py.

Run:  python3 test_replacefile.py

Copies two real disk images to /tmp/reptest/ and exercises replace on the
copies (the originals are never touched):
  * a b-tree volume (fs_version 17, LOS 3.1):
      LisaSourceCompilation/LOS_Compilation_Base.image
  * a flat-catalog volume (fs_version 14, LOS 1.0):
      /tmp/
          50MB_FreshInstallOfLOS1.0.image

Checks after every operation:
  * whole-volume tag checksums (XOR of data + tag-except-byte-11 must be 0),
  * MDDF freecount vs actual free-bit count (delta must be unchanged),
  * per-file invariants (sentry size, smallmap runs vs tag chain, tag links,
    dataused, version),
  * content round-trip (binary exact; .TEXT via lisa_text_file_to_host_text).
"""
import os
import struct
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from LisaFileSystemTool import InMemoryFileSystem, lisa_text_file_to_host_text
from LisaFileSystemToolPerFile import AddFileMixin, ReplaceFileMixin, build_lisa_text_file_data

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(REPO_ROOT, "LisaFileSystemToolPerFile.py")
WORK = "/tmp/reptest"


class FS(InMemoryFileSystem, ReplaceFileMixin):
    pass


def quiet_fs(image):
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        return FS(image)


def tag_checksums_ok(image) -> int:
    """Return the number of sectors whose stored tag checksum does not match."""
    data = open(image, "rb").read()
    # raw (non-DC42) images only: tag(20)+data(512) interleaved
    assert len(data) % 532 == 0, "expected a raw interleaved image"
    num = len(data) // 532
    bad = 0
    for sec in range(num):
        unit = sec * 532  # NOTE: see below for interleave
        # use the same interleave5 as the tool
        d = (0, 4, 8, 12, 0, 4, 8, -4, 0, 4, -8, -4, 0, -12, -8, -4)
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


def find_sfile(fs, name) -> int:
    name_u = name.upper()
    if fs.is_flat_catalog_volume():
        rc_sfile = fs._find_rootcatalog_sfile()
        rc = fs._slist_entry(rc_sfile)
        chain = fs._data_page_chain(rc[1])
        cat_data = b"".join(fs.read_sector(fs._mddf_sector_number + p) for p in chain)
        rme = fs._mddf_u16(0xC0)
        from LisaFileSystemTool import flat_catalog_hash

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
                if ce == 3:
                    return struct.unpack(">H", rec[36:38])[0]
                return None
        return None
    else:
        for node, idx, rec in fs._bt_scan_entries():
            if rec[36] != 3:
                continue
            nm = rec[3:35].split(b"\x00", 1)[0].decode("mac-roman", "replace")
            if nm.upper() == name_u:
                return struct.unpack(">H", rec[38:40])[0]
        return None


def file_info(fs, sfile):
    """(filesize, smallmap_runs, chain_pages, sentry_version)."""
    mddf = fs._mddf_sector_number
    hintaddr, fileaddr, filesize, ver = fs._slist_entry(sfile)
    chain = fs._data_page_chain(fileaddr)
    if fs.is_flat_catalog_volume():
        mo = fs._mddf_u16(0xAC)
        sm = fs.read_sector(mddf + hintaddr + mo)
        so = 0
    else:
        sm = fs.read_sector(mddf + hintaddr)
        so = fs._mddf_u16(0x114)
    sm_size = struct.unpack(">I", sm[so : so + 4])[0]
    maxent = struct.unpack(">H", sm[so + 4 : so + 6])[0]
    ecount = struct.unpack(">H", sm[so + 6 : so + 8])[0]
    runs = [
        (struct.unpack(">I", sm[so + 8 + i * 6 : so + 12 + i * 6])[0],
         struct.unpack(">H", sm[so + 12 + i * 6 : so + 14 + i * 6])[0])
        for i in range(ecount)
    ]
    map_pages = []
    for st, cnt in runs:
        map_pages.extend(range(st, st + cnt))
    return filesize, runs, chain, ver, map_pages, sm_size, maxent


def file_bytes(fs, sfile, filesize):
    mddf = fs._mddf_sector_number
    _, fileaddr, _, _ = fs._slist_entry(sfile)
    chain = fs._data_page_chain(fileaddr)
    data = b"".join(fs.read_sector(mddf + p) for p in chain)
    return data[:filesize]


def check_file_invariants(fs, sfile, expect_size, label):
    """Verify sentry/smallmap/chain/tag consistency for one file."""
    filesize, runs, chain, ver, map_pages, sm_size, maxent = file_info(fs, sfile)
    errs = []
    if filesize != expect_size:
        errs.append(f"sentry filesize {filesize} != expected {expect_size}")
    if sm_size != (expect_size + 511) // 512:
        errs.append(f"smallmap size {sm_size} != expected pages {(expect_size + 511) // 512}")
    if map_pages != chain:
        errs.append(f"smallmap pages {map_pages[:12]}... != tag chain {chain[:12]}...")
    mddf = fs._mddf_sector_number
    for i, p in enumerate(chain):
        tag = fs.read_tags_for_sector(mddf + p)
        tver = struct.unpack(">H", tag[0:2])[0]
        fid = struct.unpack(">H", tag[4:6])[0]
        rel = struct.unpack(">H", tag[12:14])[0]
        fwd = (tag[14] << 16) | (tag[15] << 8) | tag[16]
        bkw = (tag[17] << 16) | (tag[18] << 8) | tag[19]
        if tver != ver:
            errs.append(f"page {p}: tag version {tver} != sentry version {ver}")
        if fid != sfile:
            errs.append(f"page {p}: tag fileid {fid} != {sfile}")
        if rel != i:
            errs.append(f"page {p}: tag relpage {rel} != {i}")
        if i + 1 < len(chain) and fwd != chain[i + 1]:
            errs.append(f"page {p}: fwd {fwd} != {chain[i+1]}")
        if i == 0 and bkw != 0xFFFFFF:
            errs.append(f"first page {p}: bkwd {bkw} != END")
        if 0 < i and bkw != chain[i - 1]:
            errs.append(f"page {p}: bkwd {bkw} != {chain[i-1]}")
        if i == len(chain) - 1 and fwd != 0xFFFFFF:
            errs.append(f"last page {p}: fwd {fwd} != END")
    if errs:
        print(f"  [FAIL] {label}: " + "; ".join(errs[:8]))
        return False
    print(f"  [ok] {label}: {filesize} bytes, {len(chain)} pages, runs={runs}")
    return True


def run_replace(image, host, name):
    r = subprocess.run(
        [sys.executable, TOOL, "replace", image, host, name],
        capture_output=True, text=True,
    )
    return r.returncode, r.stdout, r.stderr


def run_add(image, host, name):
    r = subprocess.run(
        [sys.executable, TOOL, "add", image, host, name],
        capture_output=True, text=True,
    )
    return r.returncode, r.stdout, r.stderr


def expect_text_bytes(host_text: str) -> bytes:
    """What lisa_text_file_to_host_text() should return for this tool's output:
    CR/LF-normalized host text with \\n line endings."""
    t = host_text.replace("\r\n", "\n").replace("\r", "\n")
    return t.encode("mac-roman", "replace")


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


def main():
    os.makedirs(WORK, exist_ok=True)
    btree_img = os.path.join(WORK, "btree.image")
    flat_img = os.path.join(WORK, "flat.image")
    shutil = __import__("shutil")
    src_b = os.path.join(REPO_ROOT, "LisaSourceCompilation", "LOS_Compilation_Base.image")
    src_f = "/tmp/50MB_FreshInstallOfLOS1.0.image"
    shutil.copy(src_b, btree_img)
    shutil.copy(src_f, flat_img)

    # ---------------- baseline ----------------
    print("== baseline ==")
    b_tags = tag_checksums_ok(btree_img)
    f_tags = tag_checksums_ok(flat_img)
    b_delta = freecount_delta(btree_img)
    f_delta = freecount_delta(flat_img)
    fs = quiet_fs(btree_img)
    print(f"b-tree: fs_version={fs._fs_version}, flat={fs.is_flat_catalog_volume()}, bad tags={b_tags}, freecount delta={b_delta}")
    fs2 = quiet_fs(flat_img)
    print(f"flat:   fs_version={fs2._fs_version}, flat={fs2.is_flat_catalog_volume()}, bad tags={f_tags}, freecount delta={f_delta}")

    # host test files
    small_text = "line one\r\nline two\r\n"
    big_text = ("The quick brown fox jumps over the lazy dog.\r\n" * 200)
    with open(f"{WORK}/small.text", "w", newline="") as f:
        f.write(small_text)
    with open(f"{WORK}/big.text", "w", newline="") as f:
        f.write(big_text)
    import random
    random.seed(42)
    with open(f"{WORK}/bin_small.bin", "wb") as f:
        f.write(bytes(random.randrange(256) for _ in range(1234)))
    with open(f"{WORK}/bin_big.bin", "wb") as f:
        f.write(bytes(random.randrange(256) for _ in range(30000)))
    with open(f"{WORK}/empty.bin", "wb") as f:
        pass

    # pick victim files on the b-tree volume (dynamically: any .TEXT file and
    # any non-.TEXT file, so the harness works with any b-tree image)
    fs = quiet_fs(btree_img)
    all_names = [
        rec[3:35].split(b"\x00", 1)[0].decode("mac-roman", "replace")
        for _, _, rec in fs._bt_scan_entries()
        if rec[36] == 3
    ]
    text_names = [n for n in all_names if n.upper().endswith(".TEXT")]
    bin_names = [n for n in all_names if not n.upper().endswith(".TEXT")]
    assert text_names and bin_names, "image has no .TEXT or non-.TEXT files to test"
    victim_text_name = text_names[0]
    victim_bin_name = bin_names[0]
    victim_text = find_sfile(fs, victim_text_name)
    victim_bin = find_sfile(fs, victim_bin_name)
    print(f"b-tree victims: {victim_text_name!r} sfile={victim_text}, {victim_bin_name!r} sfile={victim_bin}")
    old_text_size = file_info(fs, victim_text)[0]
    old_bin_size = file_info(fs, victim_bin)[0]
    print(f"  old sizes: text={old_text_size}, bin={old_bin_size}")

    # ===================================================================
    print("\n== T1: b-tree .TEXT smaller ==")
    rc, out, err = run_replace(btree_img, f"{WORK}/small.text", victim_text_name)
    print(out, err)
    report(rc == 0, "exit code 0")
    fs = quiet_fs(btree_img)
    check_file_invariants(fs, victim_text, len(build_expected_on_disk(small_text)), "T1 file invariants")
    data = file_bytes(fs, victim_text, len(build_expected_on_disk(small_text)))
    roundtrip = lisa_text_file_to_host_text(data)
    report(roundtrip == expect_text_bytes(small_text), "T1 content round-trip")
    report(tag_checksums_ok(btree_img) == b_tags, "T1 whole-volume tag checksums")
    report(freecount_delta(btree_img) == b_delta, "T1 freecount delta unchanged")

    # ===================================================================
    print("\n== T2: b-tree .TEXT larger (on the same file, now small) ==")
    rc, out, err = run_replace(btree_img, f"{WORK}/big.text", victim_text_name)
    print(out, err)
    report(rc == 0, "exit code 0")
    fs = quiet_fs(btree_img)
    exp = len(build_expected_on_disk(big_text))
    check_file_invariants(fs, victim_text, exp, "T2 file invariants")
    data = file_bytes(fs, victim_text, exp)
    report(lisa_text_file_to_host_text(data) == expect_text_bytes(big_text), "T2 content round-trip")
    report(tag_checksums_ok(btree_img) == b_tags, "T2 whole-volume tag checksums")
    report(freecount_delta(btree_img) == b_delta, "T2 freecount delta unchanged")

    # ===================================================================
    print("\n== T3: b-tree binary larger ==")
    rc, out, err = run_replace(btree_img, f"{WORK}/bin_big.bin", victim_bin_name)
    print(out, err)
    report(rc == 0, "exit code 0")
    fs = quiet_fs(btree_img)
    check_file_invariants(fs, victim_bin, 30000, "T3 file invariants")
    report(file_bytes(fs, victim_bin, 30000) == open(f"{WORK}/bin_big.bin", "rb").read(), "T3 content round-trip")
    report(tag_checksums_ok(btree_img) == b_tags, "T3 whole-volume tag checksums")
    report(freecount_delta(btree_img) == b_delta, "T3 freecount delta unchanged")

    # ===================================================================
    print("\n== T4: b-tree binary smaller ==")
    rc, out, err = run_replace(btree_img, f"{WORK}/bin_small.bin", victim_bin_name)
    print(out, err)
    report(rc == 0, "exit code 0")
    fs = quiet_fs(btree_img)
    check_file_invariants(fs, victim_bin, 1234, "T4 file invariants")
    report(file_bytes(fs, victim_bin, 1234) == open(f"{WORK}/bin_small.bin", "rb").read(), "T4 content round-trip")
    report(tag_checksums_ok(btree_img) == b_tags, "T4 whole-volume tag checksums")
    report(freecount_delta(btree_img) == b_delta, "T4 freecount delta unchanged")

    # ===================================================================
    print("\n== T5: non-existent name -> exit 3, image unchanged ==")
    before = open(btree_img, "rb").read()
    rc, out, err = run_replace(btree_img, f"{WORK}/small.text", "NO/SUCH.FILE.TEXT")
    print(out, err)
    report(rc == 3, f"exit code 3 (got {rc})")
    report(open(btree_img, "rb").read() == before, "image byte-identical")

    # ===================================================================
    print("\n== T6: similar-but-nonexistent name -> exit 3, image unchanged ==")
    name_set = {n.upper() for n in all_names}
    t6_name = victim_bin_name
    while t6_name.upper() in name_set:
        t6_name += "9"
    rc, out, err = run_replace(btree_img, f"{WORK}/small.text", t6_name)
    print(out, err)
    report(rc == 3, f"exit code 3 (got {rc})")
    report(open(btree_img, "rb").read() == before, "image byte-identical")

    # ===================================================================
    print("\n== T6b: name that exists ONLY as a directory record -> exit 1 ==")
    # Craft a synthetic threadentry (eType high byte 8) named 'OBJECT' in a
    # leaf node that has a free slot, then run replace on 'OBJECT'.
    import struct as _st
    fs = quiet_fs(btree_img)
    from LisaFileSystemToolPerFile import BTREE_LEAF
    target = None
    for node, idx, rec in fs._bt_scan_entries():
        if node.kind == BTREE_LEAF and node.used + 64 <= 2032:
            target = node
            break
    assert target is not None, "no leaf with room for one more 64-byte record"
    fake = bytearray(64)
    fake[0] = 7  # name length
    fake[1:3] = _st.pack(">H", 0)  # parent = root
    fake[3:10] = b"OBJECT"
    fake[35] = 0  # forced null
    fake[36:38] = _st.pack(">H", 8 << 8)  # eType high byte = threadentry
    _st.pack_into(">H", fake, 38, 1)  # sfile: irrelevant for this test
    target.records.append(bytes(fake))
    modified = set()
    fs._bt_write_node(target, modified)
    # flush the node page
    for sn in sorted(modified):
        d = fs.read_sector(sn)
        tg = fs.read_tags_for_sector(sn)
        ck = fs.calculate_new_tag_checksum(sn)
        tg = tg[:11] + bytes([ck]) + tg[12:]
        fs._file.seek(fs._sector_tag_file_offset(sn) + 11)
        fs._file.write(bytes([ck]))
    with open(btree_img, "r+b") as fw:
        for sn in sorted(modified):
            fw.seek(fs._sector_data_file_offset(sn))
            fw.write(fs.read_sector(sn))
            fw.seek(fs._sector_tag_file_offset(sn))
            fw.write(fs.read_tags_for_sector(sn))
    fs.fix_dc42_checksum(confirm=False)
    before6b = open(btree_img, "rb").read()
    rc, out, err = run_replace(btree_img, f"{WORK}/small.text", "OBJECT")
    print(out, err)
    report(rc == 1, f"exit code 1 (got {rc})")
    report("directory" in (out + err), "error message mentions 'directory'")
    report(open(btree_img, "rb").read() == before6b, "image byte-identical (no partial writes)")

    # ===================================================================
    print("\n== T7: replace with empty file -> 0 data pages ==")
    rc, out, err = run_replace(btree_img, f"{WORK}/empty.bin", victim_bin_name)
    print(out, err)
    report(rc == 0, "exit code 0")
    fs = quiet_fs(btree_img)
    check_file_invariants(fs, victim_bin, 0, "T7 file invariants")
    report(tag_checksums_ok(btree_img) == b_tags, "T7 whole-volume tag checksums")
    report(freecount_delta(btree_img) == b_delta, "T7 freecount delta unchanged")

    # ===================================================================
    print("\n== T8: restore FSDIR.OBJ from a real file; reuse check (same size twice) ==")
    rc, out, err = run_replace(btree_img, f"{WORK}/bin_big.bin", victim_bin_name)
    report(rc == 0, "restore exit 0")
    fs = quiet_fs(btree_img)
    chain1 = file_info(fs, victim_bin)[2]
    rc, out, err = run_replace(btree_img, f"{WORK}/bin_big.bin", victim_bin_name)
    report(rc == 0, "re-replace exit 0")
    fs = quiet_fs(btree_img)
    chain2 = file_info(fs, victim_bin)[2]
    report(chain1 == chain2, f"same-size replace reuses the exact same pages ({chain1[:6]}...)")

    # ===================================================================
    print("\n== T9: case-insensitive lookup ==")
    rc, out, err = run_replace(btree_img, f"{WORK}/bin_small.bin", victim_bin_name.lower())
    print(out, err)
    report(rc == 0, f"lowercase name accepted (got {rc})")
    fs = quiet_fs(btree_img)
    check_file_invariants(fs, victim_bin, 1234, "T9 file invariants")

    # ===================================================================
    print("\n== T10: add still works (regression) ==")
    rc, out, err = run_add(btree_img, f"{WORK}/bin_small.bin", "REPLACETEST.FILE")
    print(out, err)
    report(rc == 0, f"add exit 0 (got {rc})")
    fs = quiet_fs(btree_img)
    s = find_sfile(fs, "REPLACETEST.FILE")
    report(s is not None, "added file found in catalog")
    report(file_bytes(fs, s, 1234) == open(f"{WORK}/bin_small.bin", "rb").read(), "added file content ok")

    # ===================================================================
    print("\n== T11: add duplicate still exits 3 (regression) ==")
    rc, out, err = run_add(btree_img, f"{WORK}/bin_small.bin", "REPLACETEST.FILE")
    report(rc == 3, f"add duplicate exit 3 (got {rc})")

    # =================================================================
    # flat-catalog volume
    fs = quiet_fs(flat_img)
    # list a few files
    names = []
    if fs.is_flat_catalog_volume():
        rc_sfile = fs._find_rootcatalog_sfile()
        rc = fs._slist_entry(rc_sfile)
        chain = fs._data_page_chain(rc[1])
        cat_data = b"".join(fs.read_sector(fs._mddf_sector_number + p) for p in chain)
        rme = fs._mddf_u16(0xC0)
        for i in range(rme):
            rec = cat_data[i * 54 : i * 54 + 54]
            nl, ce = rec[0], rec[34]
            if ce == 3 and nl:
                names.append(rec[1 : 1 + nl].decode("mac-roman", "replace"))
    print(f"\n== flat volume: {len(names)} files, sample: {names[:12]}")

    # ===================================================================
    print("\n== F1: flat .TEXT smaller ==")
    # pick a victim that exists
    victim = None
    for cand in names:
        if cand.upper().endswith(".TEXT"):
            victim = cand
            break
    if victim is None:
        victim = names[0] if names else None
    if victim:
        fs = quiet_fs(flat_img)
        vs = find_sfile(fs, victim)
        old_size = file_info(fs, vs)[0]
        print(f"victim: {victim} sfile={vs} old size={old_size}")
        rc, out, err = run_replace(flat_img, f"{WORK}/small.text", victim)
        print(out, err)
        report(rc == 0, f"exit code 0 (got {rc})")
        fs = quiet_fs(flat_img)
        exp = len(build_expected_on_disk(small_text))
        check_file_invariants(fs, vs, exp, "F1 file invariants")
        data = file_bytes(fs, vs, exp)
        report(lisa_text_file_to_host_text(data) == expect_text_bytes(small_text), "F1 content round-trip")
        report(tag_checksums_ok(flat_img) == f_tags, "F1 whole-volume tag checksums")
        report(freecount_delta(flat_img) == f_delta, "F1 freecount delta unchanged")
    else:
        report(False, "no victim found on flat volume")

    # ===================================================================
    print("\n== F2: flat binary larger (non-.TEXT name) ==")
    fs = quiet_fs(flat_img)
    victim2 = None
    for cand in names:
        if not cand.upper().endswith(".TEXT"):
            victim2 = cand
            break
    if victim2:
        vs2 = find_sfile(fs, victim2)
        old2 = file_info(fs, vs2)[0]
        print(f"victim: {victim2} sfile={vs2} old size={old2}")
        rc, out, err = run_replace(flat_img, f"{WORK}/bin_big.bin", victim2)
        print(out, err)
        report(rc == 0, f"exit code 0 (got {rc})")
        fs = quiet_fs(flat_img)
        check_file_invariants(fs, vs2, 30000, "F2 file invariants")
        report(file_bytes(fs, vs2, 30000) == open(f"{WORK}/bin_big.bin", "rb").read(), "F2 content round-trip")
        report(tag_checksums_ok(flat_img) == f_tags, "F2 whole-volume tag checksums")
        report(freecount_delta(flat_img) == f_delta, "F2 freecount delta unchanged")

    # ===================================================================
    print("\n== F3: flat non-existent -> exit 3, unchanged ==")
    before = open(flat_img, "rb").read()
    rc, out, err = run_replace(flat_img, f"{WORK}/small.text", "NO/SUCH.FILE")
    print(out, err)
    report(rc == 3, f"exit code 3 (got {rc})")
    report(open(flat_img, "rb").read() == before, "image byte-identical")

    # ===================================================================
    print(f"\n===== RESULTS: {PASS} passed, {FAIL} failed =====")
    sys.exit(1 if FAIL else 0)


def build_expected_on_disk(host_text: str) -> bytes:
    """Exactly what the tool writes to disk for this host text (line endings
    are normalized by build_lisa_text_file_data itself, as in add_file)."""
    return build_lisa_text_file_data(host_text.encode("ascii"))


if __name__ == "__main__":
    main()
