#!/usr/bin/env python3
"""Test harness for the put command in LisaFileSystemToolPerFile.py.

Run:  python3 test_putfile.py

Copies real disk images to /tmp/puttest/ and exercises put on the copies
(the originals are never touched):
  * a b-tree volume (fs_version 17, LOS 3.1):
      LisaSourceCompilation/LOS_Compilation_Base.image
  * a flat-catalog volume (fs_version 14, LOS 1.0):
      /tmp/50MB_FreshInstallOfLOS1.0.image   (skipped if not present)

put = add if the name is not on the volume, replace if it is; it must
return 0 on success and 1 on any failure (never 3).

Checks after every operation:
  * whole-volume tag checksums (XOR of data + tag-except-byte-11 must be 0),
  * MDDF freecount vs actual free-bit count (delta must be unchanged),
  * per-file invariants (sentry size, smallmap runs vs tag chain, tag links,
    dataused, version),
  * content round-trip (binary exact; .TEXT via lisa_text_file_to_host_text).
"""
import os
import shutil
import struct
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from LisaFileSystemTool import InMemoryFileSystem, lisa_text_file_to_host_text
from LisaFileSystemToolPerFile import ReplaceFileMixin, build_lisa_text_file_data

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(REPO_ROOT, "LisaFileSystemToolPerFile.py")
WORK = "/tmp/puttest"


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


def slist_occupied_count(image) -> int:
    """Number of slist slots with a non-zero hintaddr (i.e. 'occupied').
    put's replace path must never change this (it reuses the existing
    s-file); only the add path may grow it by one."""
    fs = quiet_fs(image)
    m = fs.read_sector(fs._mddf_sector_number)
    maxfiles = struct.unpack(">H", m[0xA0:0xA2])[0]
    n = 0
    for i in range(1, maxfiles):
        e = fs._slist_entry(i)
        if e is not None and e[0] != 0:
            n += 1
    return n


def find_sfile(fs, name) -> int:
    name_u = name.upper()
    if fs.is_flat_catalog_volume():
        rc = fs._flat_read_rootcatalog()
        if rc is None:
            return None
        cat_data, rme = rc
        slot = fs._find_flat_file_slot(cat_data, name, rme)
        if slot is None:
            return None
        centry = cat_data[slot * 54 : slot * 54 + 54]
        if centry[34] != 3:
            return None
        return struct.unpack(">H", centry[36:38])[0]
    else:
        file_matches, _ = fs._bt_find_named_entries(name)
        if file_matches:
            return struct.unpack(">H", file_matches[0][2][38:40])[0]
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


def run_put(image, host, name):
    r = subprocess.run(
        [sys.executable, TOOL, "put", image, host, name],
        capture_output=True, text=True,
    )
    return r.returncode, r.stdout, r.stderr


def expect_text_bytes(host_text: str) -> bytes:
    """What lisa_text_file_to_host_text() should return for this tool's output:
    CR/LF-normalized host text with \\n line endings."""
    t = host_text.replace("\r\n", "\n").replace("\r", "\n")
    return t.encode("mac-roman", "replace")


def build_expected_on_disk(host_text: str) -> bytes:
    """Exactly what the tool writes to disk for this host text (line endings
    are normalized by build_lisa_text_file_data itself, as in add_file)."""
    return build_lisa_text_file_data(host_text.encode("ascii"))


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


def make_host_files():
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


def test_btree():
    img = f"{WORK}/btree.image"
    src = os.path.join(REPO_ROOT, "LisaSourceCompilation", "LOS_Compilation_Base.image")
    shutil.copy(src, img)

    print("== baseline (b-tree) ==")
    b_tags = tag_checksums_ok(img)
    b_delta = freecount_delta(img)
    b_slist = slist_occupied_count(img)
    fs = quiet_fs(img)
    print(f"fs_version={fs._fs_version}, bad tags={b_tags}, freecount delta={b_delta}, slist occupied={b_slist}")

    # a victim that already exists (for the replace path)
    all_names = [
        rec[3:35].split(b"\x00", 1)[0].decode("mac-roman", "replace")
        for _, _, rec in fs._bt_scan_entries()
        if rec[36] == 3
    ]
    text_names = [n for n in all_names if n.upper().endswith(".TEXT")]
    bin_names = [n for n in all_names if not n.upper().endswith(".TEXT")]
    assert text_names, "image has no .TEXT file to test"
    assert bin_names, "image has no non-.TEXT file to test"
    victim = text_names[0]
    victim_sfile = find_sfile(fs, victim)
    victim_bin = bin_names[0]
    victim_bin_sfile = find_sfile(fs, victim_bin)
    print(f"existing victims: {victim!r} sfile={victim_sfile}, {victim_bin!r} sfile={victim_bin_sfile}")

    # ===================================================================
    print("\n== P1: put a NEW name -> adds it, exit 0 ==")
    rc, out, err = run_put(img, f"{WORK}/bin_small.bin", "PUTTEST.NEW.BIN")
    print(out, err)
    report(rc == 0, f"exit code 0 (got {rc})")
    fs = quiet_fs(img)
    s = find_sfile(fs, "PUTTEST.NEW.BIN")
    report(s is not None, "new file found in catalog")
    if s is not None:
        report(file_bytes(fs, s, 1234) == open(f"{WORK}/bin_small.bin", "rb").read(), "content ok")
    report(tag_checksums_ok(img) == b_tags, "whole-volume tag checksums")
    report(freecount_delta(img) == b_delta, "freecount delta unchanged")
    report(slist_occupied_count(img) == b_slist + 1, "slist grew by exactly one (one new s-file)")

    # ===================================================================
    print("\n== P2: put the SAME name again (different, larger content) -> replaces, exit 0 ==")
    rc, out, err = run_put(img, f"{WORK}/bin_big.bin", "PUTTEST.NEW.BIN")
    print(out, err)
    report(rc == 0, f"exit code 0 (got {rc})")
    report("Replaced" in out, "tool reports a replace (not an add)")
    fs = quiet_fs(img)
    s2 = find_sfile(fs, "PUTTEST.NEW.BIN")
    report(s2 == s, "same s-file number as before (replaced in place)")
    if s2 is not None:
        check_file_invariants(fs, s2, 30000, "P2 file invariants")
        report(file_bytes(fs, s2, 30000) == open(f"{WORK}/bin_big.bin", "rb").read(), "content ok")
    report(tag_checksums_ok(img) == b_tags, "whole-volume tag checksums")
    report(freecount_delta(img) == b_delta, "freecount delta unchanged")
    report(slist_occupied_count(img) == b_slist + 1, "slist unchanged (no new s-file, no phantom slot)")

    # ===================================================================
    print("\n== P3: put an EXISTING name with smaller .TEXT content -> replaces, exit 0 ==")
    rc, out, err = run_put(img, f"{WORK}/small.text", victim)
    print(out, err)
    report(rc == 0, f"exit code 0 (got {rc})")
    report("Replaced" in out, "tool reports a replace (not an add)")
    fs = quiet_fs(img)
    exp = len(build_expected_on_disk("line one\r\nline two\r\n"))
    check_file_invariants(fs, victim_sfile, exp, "P3 file invariants")
    data = file_bytes(fs, victim_sfile, exp)
    report(lisa_text_file_to_host_text(data) == expect_text_bytes("line one\r\nline two\r\n"),
           "P3 content round-trip")
    report(tag_checksums_ok(img) == b_tags, "whole-volume tag checksums")
    report(freecount_delta(img) == b_delta, "freecount delta unchanged")
    report(slist_occupied_count(img) == b_slist + 1, "slist unchanged (no new s-file, no phantom slot)")

    # ===================================================================
    print("\n== P4: put the EXISTING name back to a LARGER .TEXT (page growth) ==")
    rc, out, err = run_put(img, f"{WORK}/big.text", victim)
    print(out, err)
    report(rc == 0, f"exit code 0 (got {rc})")
    fs = quiet_fs(img)
    exp = len(build_expected_on_disk("The quick brown fox jumps over the lazy dog.\r\n" * 200))
    check_file_invariants(fs, victim_sfile, exp, "P4 file invariants")
    report(lisa_text_file_to_host_text(file_bytes(fs, victim_sfile, exp))
           == expect_text_bytes("The quick brown fox jumps over the lazy dog.\r\n" * 200),
           "P4 content round-trip")
    report(tag_checksums_ok(img) == b_tags, "whole-volume tag checksums")
    report(freecount_delta(img) == b_delta, "freecount delta unchanged")
    report(slist_occupied_count(img) == b_slist + 1, "slist unchanged (no new s-file, no phantom slot)")

    # ===================================================================
    print("\n== P5: put with a lowercase version of an existing (non-.TEXT) name -> replaces, exit 0 ==")
    rc, out, err = run_put(img, f"{WORK}/bin_small.bin", victim_bin.lower())
    print(out, err)
    report(rc == 0, f"lowercase name accepted (got {rc})")
    fs = quiet_fs(img)
    exp = 1234
    check_file_invariants(fs, victim_bin_sfile, exp, "P5 file invariants")
    report(file_bytes(fs, victim_bin_sfile, exp) == open(f"{WORK}/bin_small.bin", "rb").read(),
           "P5 content ok")
    report(tag_checksums_ok(img) == b_tags, "whole-volume tag checksums")
    report(freecount_delta(img) == b_delta, "freecount delta unchanged")
    report(slist_occupied_count(img) == b_slist + 1, "slist unchanged (no new s-file, no phantom slot)")

    # ===================================================================
    print("\n== P6: put with a non-existent host file -> exit 1, image unchanged ==")
    before = open(img, "rb").read()
    rc, out, err = run_put(img, f"{WORK}/NO/SUCH/HOST.FILE", "PUTTEST.NEW.BIN")
    print(out, err)
    report(rc == 1, f"exit code 1 (got {rc})")
    report(open(img, "rb").read() == before, "image byte-identical")

    # ===================================================================
    print("\n== P7: put a name that exists ONLY as a directory record -> exit 1, unchanged ==")
    fs = quiet_fs(img)
    from LisaFileSystemToolPerFile import BTREE_LEAF
    target = None
    for node, idx, rec in fs._bt_scan_entries():
        if node.kind == BTREE_LEAF and node.used + 64 <= 2032:
            target = node
            break
    assert target is not None, "no leaf with room for one more 64-byte record"
    fake = bytearray(64)
    fake[0] = 7  # name length
    fake[1:3] = struct.pack(">H", 0)  # parent = root
    fake[3:10] = b"PUTDIR"
    fake[35] = 0  # forced null
    fake[36:38] = struct.pack(">H", 8 << 8)  # eType high byte = threadentry
    struct.pack_into(">H", fake, 38, 1)  # sfile: irrelevant for this test
    target.records.append(bytes(fake))
    modified = set()
    fs._bt_write_node(target, modified)
    for sn in sorted(modified):
        d = fs.read_sector(sn)
        tg = fs.read_tags_for_sector(sn)
        ck = fs.calculate_new_tag_checksum(sn)
        tg = tg[:11] + bytes([ck]) + tg[12:]
        fs._file.seek(fs._sector_tag_file_offset(sn) + 11)
        fs._file.write(bytes([ck]))
    with open(img, "r+b") as fw:
        for sn in sorted(modified):
            fw.seek(fs._sector_data_file_offset(sn))
            fw.write(fs.read_sector(sn))
            fw.seek(fs._sector_tag_file_offset(sn))
            fw.write(fs.read_tags_for_sector(sn))
    fs.fix_dc42_checksum(confirm=False)
    before7 = open(img, "rb").read()
    rc, out, err = run_put(img, f"{WORK}/small.text", "PUTDIR")
    print(out, err)
    report(rc == 1, f"exit code 1 (got {rc})")
    report("directory" in (out + err), "error message mentions 'directory'")
    report(open(img, "rb").read() == before7, "image byte-identical (no partial writes)")

    # ===================================================================
    print("\n== P8: put a NEW .TEXT name -> exit 0, text layout on disk ==")
    rc, out, err = run_put(img, f"{WORK}/small.text", "PUTTEST.NEW.TEXT")
    print(out, err)
    report(rc == 0, f"exit code 0 (got {rc})")
    report("Added" in out, "tool reports an add (not a replace)")
    fs = quiet_fs(img)
    s = find_sfile(fs, "PUTTEST.NEW.TEXT")
    report(s is not None, "new .TEXT file found in catalog")
    if s is not None:
        exp = len(build_expected_on_disk("line one\r\nline two\r\n"))
        check_file_invariants(fs, s, exp, "P8 file invariants")
        data = file_bytes(fs, s, exp)
        report(lisa_text_file_to_host_text(data) == expect_text_bytes("line one\r\nline two\r\n"),
               "P8 content round-trip")
    report(tag_checksums_ok(img) == b_tags, "whole-volume tag checksums")
    report(freecount_delta(img) == b_delta, "freecount delta unchanged")
    report(slist_occupied_count(img) == b_slist + 2, "slist grew by one more (second new s-file)")

    # ===================================================================
    print("\n== P9: put an EMPTY file under a new name -> 0 data pages, exit 0 ==")
    rc, out, err = run_put(img, f"{WORK}/empty.bin", "PUTTEST.EMPTY.BIN")
    print(out, err)
    report(rc == 0, f"exit code 0 (got {rc})")
    fs = quiet_fs(img)
    s = find_sfile(fs, "PUTTEST.EMPTY.BIN")
    report(s is not None, "empty file found in catalog")
    if s is not None:
        check_file_invariants(fs, s, 0, "P9 file invariants")
    report(tag_checksums_ok(img) == b_tags, "whole-volume tag checksums")
    report(freecount_delta(img) == b_delta, "freecount delta unchanged")
    report(slist_occupied_count(img) == b_slist + 3, "slist grew by one more (third new s-file)")

    # ===================================================================
    print("\n== P10: add/replace/delete still work (regression) ==")
    rc, out, err = subprocess.run(
        [sys.executable, TOOL, "add", img, f"{WORK}/bin_small.bin", "PUTTEST.REG.BIN"],
        capture_output=True, text=True).returncode, None, None
    report(rc == 0, f"add exit 0 (got {rc})")
    rc, out, err = subprocess.run(
        [sys.executable, TOOL, "replace", img, f"{WORK}/bin_small.bin", "PUTTEST.REG.BIN"],
        capture_output=True, text=True).returncode, None, None
    report(rc == 0, f"replace exit 0 (got {rc})")
    rc, out, err = subprocess.run(
        [sys.executable, TOOL, "delete", img, "PUTTEST.REG.BIN"],
        capture_output=True, text=True).returncode, None, None
    report(rc == 0, f"delete exit 0 (got {rc})")
    report(tag_checksums_ok(img) == b_tags, "whole-volume tag checksums")
    report(freecount_delta(img) == b_delta, "freecount delta unchanged")
    report(slist_occupied_count(img) == b_slist + 3,
           "slist back to baseline+3 after delete (P10's add slot was released)")


def test_flat():
    src = "/tmp/50MB_FreshInstallOfLOS1.0.image"
    if not os.path.isfile(src):
        print("\n== flat volume: SKIPPED (no /tmp/50MB_FreshInstallOfLOS1.0.image) ==")
        return
    img = f"{WORK}/flat.image"
    shutil.copy(src, img)

    print("\n== baseline (flat) ==")
    f_tags = tag_checksums_ok(img)
    f_delta = freecount_delta(img)
    fs = quiet_fs(img)
    print(f"fs_version={fs._fs_version}, bad tags={f_tags}, freecount delta={f_delta}")
    rc = fs._flat_read_rootcatalog()
    assert rc is not None, "cannot read the flat rootcatalog"
    cat_data, rme = rc
    names = []
    for i in range(rme):
        rec = cat_data[i * 54 : i * 54 + 54]
        nl, ce = rec[0], rec[34]
        if ce == 3 and nl:
            names.append(rec[1 : 1 + nl].decode("mac-roman", "replace"))
    assert names, "flat volume has no files"
    victim = names[0]
    victim_sfile = find_sfile(fs, victim)
    print(f"existing victim: {victim!r} sfile={victim_sfile}")

    # ===================================================================
    print("\n== F1: flat put a NEW name -> adds it, exit 0 ==")
    rc, out, err = run_put(img, f"{WORK}/bin_small.bin", "PUTTEST.FLAT.BIN")
    print(out, err)
    report(rc == 0, f"exit code 0 (got {rc})")
    report("Added" in out, "tool reports an add (not a replace)")
    fs = quiet_fs(img)
    s = find_sfile(fs, "PUTTEST.FLAT.BIN")
    report(s is not None, "new file found in catalog")
    if s is not None:
        report(file_bytes(fs, s, 1234) == open(f"{WORK}/bin_small.bin", "rb").read(), "content ok")
    report(tag_checksums_ok(img) == f_tags, "whole-volume tag checksums")
    report(freecount_delta(img) == f_delta, "freecount delta unchanged")

    # ===================================================================
    print("\n== F2: flat put the SAME name again (larger) -> replaces, exit 0 ==")
    rc, out, err = run_put(img, f"{WORK}/bin_big.bin", "PUTTEST.FLAT.BIN")
    print(out, err)
    report(rc == 0, f"exit code 0 (got {rc})")
    report("Replaced" in out, "tool reports a replace (not an add)")
    fs = quiet_fs(img)
    s2 = find_sfile(fs, "PUTTEST.FLAT.BIN")
    report(s2 == s, "same s-file number as before (replaced in place)")
    if s2 is not None:
        check_file_invariants(fs, s2, 30000, "F2 file invariants")
        report(file_bytes(fs, s2, 30000) == open(f"{WORK}/bin_big.bin", "rb").read(), "content ok")
    report(tag_checksums_ok(img) == f_tags, "whole-volume tag checksums")
    report(freecount_delta(img) == f_delta, "freecount delta unchanged")

    # ===================================================================
    print("\n== F3: flat put an EXISTING name (smaller) -> replaces, exit 0 ==")
    rc, out, err = run_put(img, f"{WORK}/small.text", victim)
    print(out, err)
    report(rc == 0, f"exit code 0 (got {rc})")
    report("Replaced" in out, "tool reports a replace (not an add)")
    fs = quiet_fs(img)
    # the on-disk size depends on the name: .TEXT names get the text layout
    # (1024-byte header page + text pages), other names get the raw bytes
    if victim.upper().endswith(".TEXT"):
        exp = len(build_expected_on_disk("line one\r\nline two\r\n"))
    else:
        exp = len("line one\r\nline two\r\n".encode("ascii"))
    check_file_invariants(fs, victim_sfile, exp, "F3 file invariants")
    report(tag_checksums_ok(img) == f_tags, "whole-volume tag checksums")
    report(freecount_delta(img) == f_delta, "freecount delta unchanged")

    # ===================================================================
    print("\n== F4: flat put with a non-existent host file -> exit 1, unchanged ==")
    before = open(img, "rb").read()
    rc, out, err = run_put(img, f"{WORK}/NO/SUCH/HOST.FILE", "PUTTEST.FLAT.BIN")
    print(out, err)
    report(rc == 1, f"exit code 1 (got {rc})")
    report(open(img, "rb").read() == before, "image byte-identical")


def main():
    os.makedirs(WORK, exist_ok=True)
    make_host_files()
    test_btree()
    test_flat()
    print(f"\n===== RESULTS: {PASS} passed, {FAIL} failed =====")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
