#!/usr/bin/env python3
"""Test harness for the get command in LisaFileSystemToolPerFile.py.

Run:  python3 test_getfile.py

Copies real disk images to /tmp/gettest/ and exercises get on the copies
(the originals are never touched):
  * a b-tree volume (fs_version 17, LOS 3.1):
      LisaSourceCompilation/LOS_Compilation_Base.image
  * a flat-catalog volume (fs_version 14, LOS 1.0):
      /tmp/50MB_FreshInstallOfLOS1.0.image   (skipped if not present)

get = save a file from the volume to a host file (the inverse of
add/replace/put): it must return 0 on success, 3 if no file with that
name is on the volume, and 1 on any other failure. It overwrites the
destination host file if it exists, converts ".TEXT" files back to plain
host text (like the "dump" command), and never modifies the disk image
(no "image open by another process" confirmation, even without a
terminal).

Checks after every operation:
  * the disk image is byte-identical to the baseline (get is read-only),
  * whole-volume tag checksums and the MDDF freecount delta are unchanged,
  * content round-trip (binary exact; .TEXT via lisa_text_file_to_host_text).
"""
import contextlib
import io
import os
import shutil
import struct
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from LisaFileSystemTool import InMemoryFileSystem, lisa_text_file_to_host_text
from LisaFileSystemToolPerFile import FileSystemWithPerFileCommands, BTREE_LEAF

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(REPO_ROOT, "LisaFileSystemToolPerFile.py")
WORK = "/tmp/gettest"
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
        return FileSystemWithPerFileCommands(image)


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
    rc = fs._flat_read_rootcatalog()
    if rc is None:
        return []
    cat_data, _chain, rme, _size = rc
    names = []
    for i in range(rme):
        rec = cat_data[i * 54 : i * 54 + 54]
        nl, ce = rec[0], rec[34]
        if ce == 3 and nl:
            names.append(rec[1 : 1 + nl].decode("mac-roman", "replace"))
    return names


def find_sfile(fs, name):
    if fs.is_flat_catalog_volume():
        rc = fs._flat_read_rootcatalog()
        if rc is None:
            return None
        cat_data, _chain, rme, _size = rc
        slot = fs._find_flat_file_slot(cat_data, name, rme)
        if slot is None:
            return None
        centry = cat_data[slot * 54 : slot * 54 + 54]
        if centry[34] != 3:
            return None
        return struct.unpack(">H", centry[36:38])[0]
    file_matches, _ = fs._bt_find_named_entries(name)
    if file_matches:
        return struct.unpack(">H", file_matches[0][2][38:40])[0]
    return None


def on_disk_bytes(fs, sfile):
    """The file's on-disk contents: the tag chain from the slist's fileaddr,
    truncated to the sentry's filesize (the same read get_file performs,
    minus the stale-sentry fallback)."""
    _h, fileaddr, filesize, _v = fs._slist_entry(sfile)
    chain = fs._data_page_chain(fileaddr)
    data = b"".join(fs.read_sector(fs._mddf_sector_number + p) for p in chain)
    return data[:filesize]


def run_get(image, lisa_name, host, extra_args=()):
    """Run the get command with stdin closed: get must not need a terminal
    (it does not modify the image, so there is no confirmation prompt)."""
    r = subprocess.run(
        [sys.executable, TOOL, "get", image, lisa_name, host, *extra_args],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )
    return r.returncode, r.stdout, r.stderr


def check_unchanged(image, baseline, label):
    report(open(image, "rb").read() == baseline, f"{label}: image byte-identical (get is read-only)")


def test_btree():
    img = f"{WORK}/btree.image"
    shutil.copy(SRC_B, img)
    baseline = open(img, "rb").read()

    print("== baseline (b-tree) ==")
    b_tags = tag_checksums_ok(img)
    b_delta = freecount_delta(img)
    fs = quiet_fs(img)
    print(f"fs_version={fs._fs_version}, bad tags={b_tags}, freecount delta={b_delta}")

    names = file_names_btree(fs)
    text_names = [n for n in names if n.upper().endswith(".TEXT")]
    bin_names = [n for n in names if not n.upper().endswith(".TEXT")]
    assert text_names, "image has no .TEXT file to test"
    assert bin_names, "image has no non-.TEXT file to test"
    victim_bin = bin_names[0]
    victim_bin_sfile = find_sfile(fs, victim_bin)
    victim_text = text_names[0]
    victim_text_sfile = find_sfile(fs, victim_text)
    # a null .TEXT file (1024-byte header page, no text data), if present
    null_text = next((n for n in text_names
                      if fs._slist_entry(find_sfile(fs, n))[2] == 1024), None)
    print(f"victims: {victim_bin!r} sfile={victim_bin_sfile}, "
          f"{victim_text!r} sfile={victim_text_sfile}, null .TEXT: {null_text!r}")

    # ===================================================================
    print("\n== G1: get an existing binary file -> exit 0, exact content ==")
    rc, out, err = run_get(img, victim_bin, f"{WORK}/g1.bin")
    print(out, err)
    report(rc == 0, f"exit code 0 (got {rc})")
    fs = quiet_fs(img)
    report(open(f"{WORK}/g1.bin", "rb").read() == on_disk_bytes(fs, victim_bin_sfile),
           "content matches the on-disk data")
    check_unchanged(img, baseline, "G1")

    # ===================================================================
    print("\n== G2: get an existing .TEXT file -> exit 0, host text ==")
    rc, out, err = run_get(img, victim_text, f"{WORK}/g2.text")
    print(out, err)
    report(rc == 0, f"exit code 0 (got {rc})")
    fs = quiet_fs(img)
    exp = lisa_text_file_to_host_text(on_disk_bytes(fs, victim_text_sfile))
    report(open(f"{WORK}/g2.text", "rb").read() == exp,
           "content matches lisa_text_file_to_host_text() of the on-disk data")
    check_unchanged(img, baseline, "G2")

    # ===================================================================
    if null_text is not None:
        print(f"\n== G3: get a null .TEXT file ({null_text!r}) -> exit 0, empty host file ==")
        rc, out, err = run_get(img, null_text, f"{WORK}/g3.text")
        print(out, err)
        report(rc == 0, f"exit code 0 (got {rc})")
        report(os.path.getsize(f"{WORK}/g3.text") == 0, "host file is empty")
        check_unchanged(img, baseline, "G3")

    # ===================================================================
    print("\n== G4: get a file whose fwd_link chain is broken -> exit 0, full size ==")
    # TK2LIB.OBJ (s-file 34) on this image has a broken tag chain; the
    # tagged-sector fallback must recover the full file.
    victim4 = "TK2LIB.OBJ"
    s4 = find_sfile(quiet_fs(img), victim4)
    if s4 is None:
        print("  (TK2LIB.OBJ not on the volume; skipped)")
    else:
        size4 = quiet_fs(img)._slist_entry(s4)[2]
        rc, out, err = run_get(img, victim4, f"{WORK}/g4.obj")
        print(out, err)
        report(rc == 0, f"exit code 0 (got {rc})")
        report(os.path.getsize(f"{WORK}/g4.obj") == size4,
               f"full {size4} bytes recovered (chain fallback)")
        check_unchanged(img, baseline, "G4")

    # ===================================================================
    print("\n== G5: get a non-existent name -> exit 3, no host file created ==")
    dest5 = f"{WORK}/g5.bin"
    if os.path.exists(dest5):
        os.remove(dest5)
    rc, out, err = run_get(img, "NO.SUCH.GET.FILE", dest5)
    print(out, err)
    report(rc == 3, f"exit code 3 (got {rc})")
    report(not os.path.exists(dest5), "no host file was created")
    check_unchanged(img, baseline, "G5")

    # ===================================================================
    print("\n== G6: get a directory-style name with no file record -> exit 3 ==")
    slashed = next((n for n in names if "/" in n), None)
    if slashed is None:
        print("  (no file name contains '/'; skipped)")
    else:
        t6 = slashed.split("/")[0]  # a top-level directory name
        fsx = quiet_fs(img)
        assert find_sfile(fsx, t6) is None, f"{t6!r} unexpectedly resolves to a file"
        rc, out, err = run_get(img, t6, f"{WORK}/g6.bin")
        print(out, err)
        report(rc == 3, f"exit code 3 (got {rc})")
        check_unchanged(img, baseline, "G6")

    # ===================================================================
    print("\n== G7: get a name that exists ONLY as a directory record -> exit 1 ==")
    fs = quiet_fs(img)
    target = None
    for node, idx, rec in fs._bt_scan_entries():
        if node.kind == BTREE_LEAF and node.used + 64 <= 2032:
            target = node
            break
    assert target is not None, "no leaf with room for one more 64-byte record"
    fake = bytearray(64)
    fake[0] = 7  # name length
    fake[1:3] = struct.pack(">H", 0)  # parent = root
    fake[3:10] = b"GETDIR"
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
    baseline7 = open(img, "rb").read()
    rc, out, err = run_get(img, "GETDIR", f"{WORK}/g7.bin")
    print(out, err)
    report(rc == 1, f"exit code 1 (got {rc})")
    report("directory" in (out + err), "error message mentions 'directory'")
    report(not os.path.exists(f"{WORK}/g7.bin"), "no host file was created")
    report(open(img, "rb").read() == baseline7, "image byte-identical")
    # from here on, the injected directory record is part of the baseline
    baseline = baseline7

    # ===================================================================
    print("\n== G8: get with a lowercase version of an existing name -> exit 0 ==")
    rc, out, err = run_get(img, victim_bin.lower(), f"{WORK}/g8.bin")
    print(out, err)
    report(rc == 0, f"lowercase name accepted (got {rc})")
    report(open(f"{WORK}/g8.bin", "rb").read() == on_disk_bytes(quiet_fs(img), victim_bin_sfile),
           "content matches the on-disk data")
    check_unchanged(img, baseline, "G8")

    # ===================================================================
    print("\n== G9: get overwrites an existing destination host file ==")
    with open(f"{WORK}/g9.bin", "wb") as f:
        f.write(b"OLD CONTENT that is much longer than the file being saved" * 10)
    rc, out, err = run_get(img, victim_bin, f"{WORK}/g9.bin")
    print(out, err)
    report(rc == 0, f"exit code 0 (got {rc})")
    report(open(f"{WORK}/g9.bin", "rb").read() == on_disk_bytes(quiet_fs(img), victim_bin_sfile),
           "destination overwritten with the file's data")
    check_unchanged(img, baseline, "G9")

    # ===================================================================
    print("\n== G10: get to a host path in a missing directory -> exit 1 ==")
    rc, out, err = run_get(img, victim_bin, f"{WORK}/no/such/dir/g10.bin")
    print(out, err)
    report(rc == 1, f"exit code 1 (got {rc})")
    check_unchanged(img, baseline, "G10")

    # ===================================================================
    print("\n== G11: get with a wrong argument count -> exit 1, usage printed ==")
    r = subprocess.run(
        [sys.executable, TOOL, "get", img, victim_bin],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )
    print(r.stdout, r.stderr)
    report(r.returncode == 1, f"exit code 1 (got {r.returncode})")
    report("Usage:" in (r.stdout + r.stderr), "usage message printed")

    # ===================================================================
    print("\n== G12: add/replace/put/delete still work (regression) ==")
    with open(f"{WORK}/rt.bin", "wb") as f:
        f.write(bytes(range(256)) * 8)
    rc = subprocess.run([sys.executable, TOOL, "add", img, f"{WORK}/rt.bin", "GETST/RT.BIN"],
                        capture_output=True, text=True).returncode
    report(rc == 0, f"add exit 0 (got {rc})")
    rc = subprocess.run([sys.executable, TOOL, "put", img, f"{WORK}/rt.bin", "GETST/RT.BIN"],
                        capture_output=True, text=True).returncode
    report(rc == 0, f"put exit 0 (got {rc})")
    rc = subprocess.run([sys.executable, TOOL, "get", img, "GETST/RT.BIN", f"{WORK}/rt.out"],
                        capture_output=True, text=True, stdin=subprocess.DEVNULL).returncode
    report(rc == 0, f"get of the just-added file exit 0 (got {rc})")
    report(open(f"{WORK}/rt.out", "rb").read() == open(f"{WORK}/rt.bin", "rb").read(),
           "add -> get round trip is byte-exact")
    rc = subprocess.run([sys.executable, TOOL, "delete", img, "GETST/RT.BIN"],
                        capture_output=True, text=True).returncode
    report(rc == 0, f"delete exit 0 (got {rc})")


def test_flat():
    if not os.path.isfile(SRC_F):
        print("\n== flat volume: SKIPPED (no /tmp/50MB_FreshInstallOfLOS1.0.image) ==")
        return
    img = f"{WORK}/flat.image"
    shutil.copy(SRC_F, img)
    baseline = open(img, "rb").read()

    print("\n== baseline (flat) ==")
    f_tags = tag_checksums_ok(img)
    f_delta = freecount_delta(img)
    fs = quiet_fs(img)
    print(f"fs_version={fs._fs_version}, bad tags={f_tags}, freecount delta={f_delta}")
    fnames = file_names_flat(fs)
    assert fnames, "flat volume has no files"
    victim = next((n for n in fnames if n.upper().endswith(".TEXT")), fnames[0])
    victim_sfile = find_sfile(fs, victim)
    print(f"victim: {victim!r} sfile={victim_sfile}")

    # ===================================================================
    print("\n== F1: flat get of an existing file -> exit 0, exact content ==")
    rc, out, err = run_get(img, victim, f"{WORK}/f1.out")
    print(out, err)
    report(rc == 0, f"exit code 0 (got {rc})")
    fs = quiet_fs(img)
    on_disk = on_disk_bytes(fs, victim_sfile)
    if victim.upper().endswith(".TEXT"):
        exp = lisa_text_file_to_host_text(on_disk)
    else:
        exp = on_disk
    report(open(f"{WORK}/f1.out", "rb").read() == exp, "content matches the on-disk data")
    check_unchanged(img, baseline, "F1")
    report(tag_checksums_ok(img) == f_tags, "whole-volume tag checksums")
    report(freecount_delta(img) == f_delta, "freecount delta unchanged")

    # ===================================================================
    print("\n== F2: flat get of a non-file catalog entry -> exit 1, unchanged ==")
    rc_ent = fs._flat_read_rootcatalog()
    cat_data, _chain, rme, _size = rc_ent
    dir_name = None
    for i in range(rme):
        rec = cat_data[i * 54 : i * 54 + 54]
        nl, ce = rec[0], rec[34]
        if ce != 3 and ce != 0 and ce != 7 and nl:  # a directory or other non-file entry
            dir_name = rec[1 : 1 + nl].decode("mac-roman", "replace")
            break
    if dir_name is None:
        print("  (flat volume has no non-file centry; skipped)")
    else:
        rc, out, err = run_get(img, dir_name, f"{WORK}/f2.out")
        print(out, err)
        report(rc == 1, f"exit code 1 (got {rc})")
        report(not os.path.exists(f"{WORK}/f2.out"), "no host file was created")
        check_unchanged(img, baseline, "F2")

    # ===================================================================
    print("\n== F3: flat get of a non-existent name -> exit 3, unchanged ==")
    rc, out, err = run_get(img, "NO.SUCH.FLAT.FILE", f"{WORK}/f3.out")
    print(out, err)
    report(rc == 3, f"exit code 3 (got {rc})")
    check_unchanged(img, baseline, "F3")


def main():
    os.makedirs(WORK, exist_ok=True)
    test_btree()
    test_flat()
    print(f"\n===== RESULTS: {PASS} passed, {FAIL} failed =====")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
