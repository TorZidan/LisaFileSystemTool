#!/usr/bin/env python3
"""
run_tests.py -- test suite for LisaFileSystemTool.py and LisaFileSystemToolPerFile.py.

Tests every supported command of both tools against each of the five disk
image files in this folder:

  LisaFileSystemTool.py:
      deserialize, info, list, visualize, dump, dump-flatten,
      fix-dc42-checksum, rename-volume
  LisaFileSystemToolPerFile.py:
      add, replace, put, delete, get

The commands are exercised through the real command-line interface (the tools
are started as subprocesses). Every command runs against a fresh copy of the
image in /tmp/LisaFileSystemToolTests/<image name>/ -- the original image
files in this folder are never modified.

For each image the tests check:
  * exit codes (0 = success, 1 = failure, 3 = "nothing to do" for the
    PerFile commands),
  * expected output lines,
  * side effects: files written to /tmp/LisaFileSystemDump by 'dump',
    host files written by 'get', the new volume name after 'rename-volume',
    the corrected DC42 header checksums after 'fix-dc42-checksum', and the
    add/replace/put/delete changes made visible by 'get' and 'list'.

Known limitation of the tools that the tests take into account:
  * 'add' (and therefore the add path of 'put') is not supported on
    b-tree-catalog volumes with 12-byte tags (floppy images); for those
    images the tests expect the documented error (exit code 1).

Usage:
    python3 tests/run_tests.py                 # run all five images
    python3 tests/run_tests.py <image> [...]   # run only the given image(s)

Cleanup: /tmp/LisaFileSystemDump is removed after every image's tests, and
the working directory of an image is removed once all of its checks have
passed. If any check for an image fails, that image's working directory is
KEPT in /tmp/LisaFileSystemToolTests/<image name>/ so the modified copies
can be inspected; the next run starts by wiping the whole tree anyway.

Exit status: 0 if every test passed, 1 otherwise.
"""

import os
import re
import shutil
import subprocess
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
TOOL = os.path.join(REPO_ROOT, "LisaFileSystemTool.py")
TOOL_PERFILE = os.path.join(REPO_ROOT, "LisaFileSystemToolPerFile.py")
PYTHON = sys.executable or "python3"

# The 'dump'/'dump-flatten' commands always write here (hard-coded in
# LisaFileSystemTool.py), so the tests must clean it up between runs:
DUMP_DIR = "/tmp/LisaFileSystemDump"
# All image copies and host files used by the tests live here:
WORK_ROOT = "/tmp/LisaFileSystemToolTests"

IMAGES = [
    "floppy-sony400k-LisaDraw3.0.dc42",
    "floppy-twiggy-LisaDraw1.0.dc42",
    "profile-LOS1.0.dc42",
    "profile-LOS2.0andWorkshop2.0andBasicPlus2.0.dc42",
    "profile-LOS3.1.image",
]

DC42_DATA_CHECKSUM_OFFSET = 0x48  # stored data checksum in the DC42 header
NEW_VOLUME_NAME = "TestVol123"
TEXT_CONTENT = b"line one\nline two\n"  # host text for the .text round-trip tests
BIN_CONTENT_ADD = b"ADD CONTENT " * 256  # 2048 bytes
BIN_CONTENT_REPLACE = b"REPLACE CONTENT " * 128  # 2048 bytes
BIN_CONTENT_PUT = b"PUT CONTENT " * 64  # 1024 bytes
TOOL_TIMEOUT = 600  # seconds per tool invocation


# ---------------------------------------------------------------------------
# small test harness
# ---------------------------------------------------------------------------

class TestResult:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.failures = []  # (image, test name, detail)

    def check(self, image, name, ok, detail=""):
        if ok:
            self.passed += 1
            print(f"    PASS  {name}")
        else:
            self.failed += 1
            self.failures.append((image, name, detail))
            print(f"    FAIL  {name}")
            if detail:
                for line in str(detail).splitlines()[-12:]:
                    print(f"            | {line}")


def run_tool(tool, args, stdin=None, timeout=TOOL_TIMEOUT):
    """Run a tool command; return (exit_code, combined stdout+stderr).

    stdin is a string to feed to the tool (used to answer the y/N
    confirmation prompts), or None for a closed stdin.
    """
    try:
        proc = subprocess.run(
            [PYTHON, tool] + args,
            input=stdin.encode("utf-8") if stdin is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return -1, f"TIMEOUT after {timeout} seconds"
    except Exception as e:  # noqa: BLE001 - report any spawn problem as a failure
        return -1, f"could not run the tool: {e}"


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def clear_dump_dir():
    shutil.rmtree(DUMP_DIR, ignore_errors=True)


def list_dump_files():
    """Relative paths (with '/' separators) of all files in DUMP_DIR."""
    files = []
    if os.path.isdir(DUMP_DIR):
        for dirpath, _dirnames, filenames in os.walk(DUMP_DIR):
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                files.append(os.path.relpath(full, DUMP_DIR).replace(os.sep, "/"))
    return sorted(files)


def dump_file_content(name):
    return read_bytes(os.path.join(DUMP_DIR, *name.split("/")))


# ---------------------------------------------------------------------------
# image properties, parsed from the 'info' output
# ---------------------------------------------------------------------------

class ImageInfo:
    def __init__(self, info_output):
        self.is_dc42 = "File format: DC42" in info_output
        m = re.search(r"Sector tags size: (\d+)", info_output)
        self.tag_size = int(m.group(1)) if m else None
        m = re.search(r"File system version .*? is (\d+) \(", info_output)
        self.fs_version = int(m.group(1)) if m else None
        self.is_flat = self.fs_version in (14, 15)
        m = re.search(r"Computed Data Checksum:\s+0x[0-9a-f]+ : (OK|BAD!)", info_output)
        self.data_checksum_ok = bool(m and m.group(1) == "OK")
        m = re.search(r"Computed Tag Checksum:\s+0x[0-9a-f]+ : (OK|BAD!)", info_output)
        self.tag_checksum_ok = bool(m and m.group(1) == "OK")
        self.checksums_ok = self.data_checksum_ok and self.tag_checksum_ok
        # 'add' (and the add path of 'put') is not supported on b-tree
        # volumes with 12-byte tags (floppy images):
        self.add_supported = self.is_flat or self.tag_size == 20

    def describe(self):
        catalog = "flat catalog" if self.is_flat else "b-tree catalog"
        kind = "DC42" if self.is_dc42 else "raw ProFile"
        return (
            f"{kind}, {self.tag_size}-byte tags, fs_version {self.fs_version} "
            f"({catalog})"
        )


# ---------------------------------------------------------------------------
# tests for LisaFileSystemTool.py
# ---------------------------------------------------------------------------

def test_info(results, image, info, copy):
    rc, out = run_tool(TOOL, ["info", copy])
    results.check(image, "info: exit code 0", rc == 0, out[-2000:])
    if info.is_dc42:
        results.check(image, "info: reports DC42 format", "File format: DC42" in out)
    else:
        results.check(
            image,
            "info: reports raw ProFile format",
            "File format: Raw ProFile" in out,
            out[-2000:],
        )
    results.check(
        image,
        "info: reports the file system version",
        f"is {info.fs_version} (" in out,
        out[-2000:],
    )


def test_list(results, image, info, copy):
    rc, out = run_tool(TOOL, ["list", copy])
    results.check(image, "list: exit code 0", rc == 0, out[-2000:])
    results.check(
        image,
        "list: prints a file list",
        "Found" in out and "file" in out.lower(),
        out[-2000:],
    )


def test_visualize(results, image, info, copy):
    rc, out = run_tool(TOOL, ["visualize", copy])
    results.check(image, "visualize: exit code 0", rc == 0, out[-2000:])
    map_lines = re.findall(r"^\d+: [BLMPSCH987654321?.]+", out, re.M)
    results.check(
        image,
        "visualize: prints one map line per 64 sectors",
        len(map_lines) >= 1 and "Sector counts:" in out,
        out[-2000:],
    )


def test_dump(results, image, info, copy):
    clear_dump_dir()
    rc, out = run_tool(TOOL, ["dump", copy])
    results.check(image, "dump: exit code 0", rc == 0, out[-2000:])
    files = list_dump_files()
    results.check(
        image,
        "dump: wrote file(s) to /tmp/LisaFileSystemDump",
        len(files) > 0,
        f"{len(files)} file(s) found; output: {out[-2000:]}",
    )


def test_dump_flatten(results, image, info, copy):
    clear_dump_dir()
    rc, out = run_tool(TOOL, ["dump-flatten", copy])
    results.check(image, "dump-flatten: exit code 0", rc == 0, out[-2000:])
    files = list_dump_files()
    results.check(
        image,
        "dump-flatten: wrote file(s) to /tmp/LisaFileSystemDump",
        len(files) > 0,
        f"{len(files)} file(s) found; output: {out[-2000:]}",
    )
    results.check(
        image,
        "dump-flatten: no subfolders were created",
        all("/" not in f for f in files),
        str(files[:20]),
    )


def test_fix_dc42_checksum(results, image, info, copy):
    """Run 'fix-dc42-checksum' on a pristine copy: it must either report
    that there is nothing to do, or (for an image whose stored checksums are
    wrong) fix them after the y/N confirmation."""
    if not info.is_dc42:
        rc, out = run_tool(TOOL, ["fix-dc42-checksum", copy])
        results.check(
            image, "fix-dc42-checksum (raw image): exit code 0", rc == 0, out[-2000:]
        )
        results.check(
            image,
            "fix-dc42-checksum (raw image): nothing to do",
            "Not a DC42 file" in out,
            out[-2000:],
        )
        return
    if info.checksums_ok:
        # Already correct: no prompt is expected, so run with closed stdin.
        rc, out = run_tool(TOOL, ["fix-dc42-checksum", copy], stdin="")
        results.check(
            image, "fix-dc42-checksum (correct): exit code 0", rc == 0, out[-2000:]
        )
        results.check(
            image,
            "fix-dc42-checksum (correct): reports nothing to do",
            "already correct" in out,
            out[-2000:],
        )
    else:
        rc, out = run_tool(TOOL, ["fix-dc42-checksum", copy], stdin="y\n")
        results.check(
            image, "fix-dc42-checksum (wrong): exit code 0", rc == 0, out[-2000:]
        )
        results.check(
            image,
            "fix-dc42-checksum (wrong): checksum(s) updated",
            "Updated DC42 header" in out,
            out[-2000:],
        )
        rc2, out2 = run_tool(TOOL, ["info", copy])
        results.check(
            image,
            "fix-dc42-checksum (wrong): 'info' now reports OK checksums",
            re.search(r"Computed Data Checksum:\s+0x[0-9a-f]+ : OK", out2)
            and re.search(r"Computed Tag Checksum:\s+0x[0-9a-f]+ : OK", out2),
            out2[-2000:],
        )


def test_fix_dc42_checksum_corrupted(results, image, info, copy):
    """Corrupt the stored DC42 data checksum in the copy and check that
    'fix-dc42-checksum' detects and repairs it. (DC42 images only; the copy
    is the one already used by test_fix_dc42_checksum, so its checksums are
    known-good at this point.)"""
    if not info.is_dc42:
        return
    with open(copy, "r+b") as f:
        f.seek(DC42_DATA_CHECKSUM_OFFSET)
        stored = bytearray(f.read(4))
        stored[0] ^= 0xFF
        f.seek(DC42_DATA_CHECKSUM_OFFSET)
        f.write(bytes(stored))
    rc, out = run_tool(TOOL, ["fix-dc42-checksum", copy], stdin="y\n")
    results.check(
        image, "fix-dc42-checksum (corrupted): exit code 0", rc == 0, out[-2000:]
    )
    results.check(
        image,
        "fix-dc42-checksum (corrupted): data checksum updated",
        "Updated DC42 header data checksum" in out,
        out[-2000:],
    )
    rc2, out2 = run_tool(TOOL, ["info", copy])
    results.check(
        image,
        "fix-dc42-checksum (corrupted): 'info' now reports OK checksums",
        re.search(r"Computed Data Checksum:\s+0x[0-9a-f]+ : OK", out2)
        and re.search(r"Computed Tag Checksum:\s+0x[0-9a-f]+ : OK", out2),
        out2[-2000:],
    )


def test_rename_volume(results, image, info, copy):
    rc, out = run_tool(TOOL, ["rename-volume", copy, NEW_VOLUME_NAME], stdin="y\n")
    results.check(
        image, "rename-volume: exit code 0", rc == 0, out[-2000:]
    )
    rc2, out2 = run_tool(TOOL, ["info", copy])
    results.check(
        image,
        "rename-volume: new name visible in 'info'",
        f"(string '{NEW_VOLUME_NAME}')" in out2,
        out2[-2000:],
    )
    rc3, out3 = run_tool(TOOL, ["rename-volume", copy, NEW_VOLUME_NAME], stdin="y\n")
    results.check(
        image, "rename-volume (same name): exit code 0", rc3 == 0, out3[-2000:]
    )
    results.check(
        image,
        "rename-volume (same name): reports nothing to do",
        "already named" in out3,
        out3[-2000:],
    )
    rc4, out4 = run_tool(TOOL, ["rename-volume", copy, "bad/name"], stdin="y\n")
    results.check(
        image, "rename-volume (name with '/'): exit code 1", rc4 == 1, out4[-2000:]
    )
    results.check(
        image,
        "rename-volume (name with '/'): error message",
        "ERROR" in out4,
        out4[-2000:],
    )
    rc5, out5 = run_tool(TOOL, ["rename-volume", copy, "a" * 34], stdin="y\n")
    results.check(
        image, "rename-volume (34 characters): exit code 1", rc5 == 1, out5[-2000:]
    )
    results.check(
        image,
        "rename-volume (34 characters): error message",
        "ERROR" in out5,
        out5[-2000:],
    )


def test_deserialize(results, image, info, copy):
    rc, out = run_tool(TOOL, ["deserialize", copy], stdin="y\n")
    results.check(image, "deserialize: exit code 0", rc == 0, out[-2000:])
    rc2, out2 = run_tool(TOOL, ["deserialize", copy], stdin="y\n")
    results.check(
        image, "deserialize (2nd run): exit code 0", rc2 == 0, out2[-2000:]
    )
    results.check(
        image,
        "deserialize (2nd run): no protected files left",
        "No protected files found" in out2,
        out2[-2000:],
    )


# ---------------------------------------------------------------------------
# tests for LisaFileSystemToolPerFile.py
# ---------------------------------------------------------------------------

def discover_files(results, image, copy):
    """Run 'dump' on the copy (dump never modifies the image) and pick an
    existing non-text file and an existing .text file for the tests."""
    clear_dump_dir()
    rc, out = run_tool(TOOL, ["dump", copy])
    results.check(
        image, "perfile setup: 'dump' for file discovery", rc == 0, out[-2000:]
    )
    names = list_dump_files()
    binary_name = None
    text_name = None
    for name in names:
        # Skip the volume's special system files ({!CATALOG}, {!SaveDesktop},
        # rootcatalog, ...): the tests modify the chosen file, and system
        # files are not a realistic target for that.
        if name.startswith("{!") or name.upper() == "ROOTCATALOG":
            continue
        path = os.path.join(DUMP_DIR, *name.split("/"))
        if os.path.getsize(path) == 0:
            continue
        if name.upper().endswith(".TEXT"):
            if text_name is None:
                text_name = name
        elif binary_name is None:
            binary_name = name
    results.check(
        image,
        "perfile setup: found an existing non-text file",
        binary_name is not None,
        str(names[:20]),
    )
    return binary_name, text_name, names


def test_perfile(results, image, info, copy, workdir):
    binary_name, text_name, _names = discover_files(results, image, copy)
    if binary_name is None:
        return

    host_bin_add = os.path.join(workdir, "host-bin-add.dat")
    host_bin_replace = os.path.join(workdir, "host-bin-replace.dat")
    host_bin_put = os.path.join(workdir, "host-bin-put.dat")
    host_text = os.path.join(workdir, "host-text.txt")
    with open(host_bin_add, "wb") as f:
        f.write(BIN_CONTENT_ADD)
    with open(host_bin_replace, "wb") as f:
        f.write(BIN_CONTENT_REPLACE)
    with open(host_bin_put, "wb") as f:
        f.write(BIN_CONTENT_PUT)
    with open(host_text, "wb") as f:
        f.write(TEXT_CONTENT)

    def get_to(name, tag):
        host_out = os.path.join(workdir, f"get-{tag}.out")
        rc, out = run_tool(TOOL_PERFILE, ["get", copy, name, host_out])
        results.check(
            image, f"get '{name}': exit code 0", rc == 0, out[-2000:]
        )
        return rc, host_out, out

    # ---- get ----------------------------------------------------------------
    rc, host_out, out = get_to(binary_name, "binary")
    results.check(
        image,
        "get: host file content matches the 'dump' of the same file",
        os.path.isfile(host_out)
        and read_bytes(host_out) == dump_file_content(binary_name),
    )
    rc, out = run_tool(
        TOOL_PERFILE,
        ["get", copy, "no-such-file-zzz", os.path.join(workdir, "get-missing.out")],
    )
    results.check(
        image, "get (missing file): exit code 3", rc == 3, out[-2000:]
    )
    if text_name is not None:
        rc, host_out, out = get_to(text_name, "text")
        results.check(
            image,
            "get (.text file): host file content matches the 'dump'",
            os.path.isfile(host_out)
            and read_bytes(host_out) == dump_file_content(text_name),
        )

    # ---- add ----------------------------------------------------------------
    if info.add_supported:
        rc, out = run_tool(TOOL_PERFILE, ["add", copy, host_bin_add, "ztest-bin-file"],
                           stdin="y\n")
        results.check(image, "add (new binary file): exit code 0", rc == 0, out[-2000:])
        rc, host_out, out = get_to("ztest-bin-file", "added-bin")
        results.check(
            image,
            "add: file can be read back with the same content",
            os.path.isfile(host_out) and read_bytes(host_out) == BIN_CONTENT_ADD,
        )
        rc, out = run_tool(TOOL_PERFILE, ["add", copy, host_bin_add, "ztest-bin-file"],
                           stdin="y\n")
        results.check(
            image, "add (existing name): exit code 3", rc == 3, out[-2000:]
        )
        rc, out = run_tool(TOOL_PERFILE, ["add", copy, host_text, "ztest-file.text"],
                           stdin="y\n")
        results.check(
            image, "add (new .text file): exit code 0", rc == 0, out[-2000:]
        )
        rc, host_out, out = get_to("ztest-file.text", "added-text")
        results.check(
            image,
            "add (.text file): read back as the exact host text",
            os.path.isfile(host_out) and read_bytes(host_out) == TEXT_CONTENT,
        )
    else:
        rc, out = run_tool(TOOL_PERFILE, ["add", copy, host_bin_add, "ztest-bin-file"],
                           stdin="y\n")
        results.check(
            image,
            "add (12-byte-tag b-tree volume): exit code 1",
            rc == 1,
            out[-2000:],
        )
        results.check(
            image,
            "add (12-byte-tag b-tree volume): documented error message",
            "only 20-byte-tag" in out,
            out[-2000:],
        )

    # ---- replace --------------------------------------------------------------
    rc, out = run_tool(TOOL_PERFILE, ["replace", copy, host_bin_replace, binary_name],
                       stdin="y\n")
    results.check(
        image, f"replace (existing file '{binary_name}'): exit code 0",
        rc == 0, out[-2000:],
    )
    rc, host_out, out = get_to(binary_name, "replaced")
    results.check(
        image,
        "replace: file can be read back with the new content",
        os.path.isfile(host_out) and read_bytes(host_out) == BIN_CONTENT_REPLACE,
    )
    rc, out = run_tool(
        TOOL_PERFILE,
        ["replace", copy, host_bin_replace, "no-such-file-zzz"],
        stdin="y\n",
    )
    results.check(
        image, "replace (missing file): exit code 3", rc == 3, out[-2000:]
    )

    # ---- put ------------------------------------------------------------------
    if info.add_supported:
        rc, out = run_tool(TOOL_PERFILE, ["put", copy, host_bin_add, "ztest-put-new"],
                           stdin="y\n")
        results.check(
            image, "put (new name, add path): exit code 0", rc == 0, out[-2000:]
        )
    rc, out = run_tool(TOOL_PERFILE, ["put", copy, host_bin_put, binary_name],
                       stdin="y\n")
    results.check(
        image, "put (existing name, replace path): exit code 0", rc == 0, out[-2000:]
    )
    rc, host_out, out = get_to(binary_name, "put")
    results.check(
        image,
        "put: file can be read back with the new content",
        os.path.isfile(host_out) and read_bytes(host_out) == BIN_CONTENT_PUT,
    )
    rc, out = run_tool(
        TOOL_PERFILE,
        ["put", copy, os.path.join(workdir, "no-such-host-file"), "ztest-put-bad"],
        stdin="y\n",
    )
    results.check(
        image, "put (missing host file): exit code 1", rc == 1, out[-2000:]
    )

    # ---- delete -----------------------------------------------------------------
    victim = "ztest-bin-file" if info.add_supported else binary_name
    rc, out = run_tool(TOOL_PERFILE, ["delete", copy, victim], stdin="y\n")
    results.check(image, f"delete ('{victim}'): exit code 0", rc == 0, out[-2000:])
    rc, out = run_tool(
        TOOL_PERFILE, ["get", copy, victim, os.path.join(workdir, "get-deleted.out")]
    )
    results.check(
        image, "delete: file is gone (get exit code 3)", rc == 3, out[-2000:]
    )
    rc, out = run_tool(TOOL_PERFILE, ["delete", copy, victim], stdin="y\n")
    results.check(
        image, "delete (file already gone): exit code 3", rc == 3, out[-2000:]
    )

    # ---- the volume must still be consistent after all the modifications --------
    rc, out = run_tool(TOOL, ["list", copy])
    results.check(
        image,
        "after all per-file operations: 'list' still works",
        rc == 0 and "Found" in out,
        out[-2000:],
    )
    rc, out = run_tool(TOOL, ["info", copy])
    results.check(
        image,
        "after all per-file operations: 'info' still works",
        rc == 0,
        out[-2000:],
    )
    if info.is_dc42:
        results.check(
            image,
            "after all per-file operations: DC42 header checksums still OK",
            re.search(r"Computed Data Checksum:\s+0x[0-9a-f]+ : OK", out)
            and re.search(r"Computed Tag Checksum:\s+0x[0-9a-f]+ : OK", out),
            out[-2000:],
        )


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------

def fresh_copy(workdir, src, name):
    dst = os.path.join(workdir, name)
    shutil.copy2(src, dst)
    return dst


def run_image_tests(results, image):
    src = os.path.join(TESTS_DIR, image)
    if not os.path.isfile(src):
        results.check(image, "image file present in tests/", False,
                      f"{src} not found")
        return

    stem = os.path.splitext(image)[0]
    ext = os.path.splitext(image)[1]
    workdir = os.path.join(WORK_ROOT, stem)
    shutil.rmtree(workdir, ignore_errors=True)
    os.makedirs(workdir, exist_ok=True)

    print(f"\n====================================================================")
    print(f"Image: {image}")
    print(f"Working directory (all copies): {workdir}")
    print(f"====================================================================")

    failed_before = results.failed
    try:
        # Base copy for the read-only commands:
        base_copy = fresh_copy(workdir, src, "base" + ext)
        rc, out = run_tool(TOOL, ["info", base_copy])
        if rc != 0:
            results.check(image, "setup: 'info' on the base copy", False, out[-2000:])
            return
        info = ImageInfo(out)
        print(f"Detected: {info.describe()}")

        print("  [LisaFileSystemTool.py]")
        test_info(results, image, info, base_copy)
        test_list(results, image, info, base_copy)
        test_visualize(results, image, info, base_copy)
        test_dump(results, image, info, base_copy)
        test_dump_flatten(results, image, info, base_copy)

        fix_copy = fresh_copy(workdir, src, "fix" + ext)
        test_fix_dc42_checksum(results, image, info, fix_copy)
        test_fix_dc42_checksum_corrupted(results, image, info, fix_copy)

        rename_copy = fresh_copy(workdir, src, "rename" + ext)
        test_rename_volume(results, image, info, rename_copy)

        deserialize_copy = fresh_copy(workdir, src, "deserialize" + ext)
        test_deserialize(results, image, info, deserialize_copy)

        print("  [LisaFileSystemToolPerFile.py]")
        perfile_copy = fresh_copy(workdir, src, "perfile" + ext)
        test_perfile(results, image, info, perfile_copy, workdir)
    finally:
        # Always remove the shared dump directory; remove this image's
        # working directory when all of its checks passed, but keep it when
        # something failed so the modified copies can be inspected.
        clear_dump_dir()
        if results.failed == failed_before:
            shutil.rmtree(workdir, ignore_errors=True)
        else:
            print(f"  NOTE: kept {workdir} for inspection (some checks failed)")


def main(argv):
    images = argv if argv else IMAGES
    unknown = [i for i in images if i not in IMAGES]
    if unknown:
        print(f"ERROR: unknown image file name(s): {', '.join(unknown)}")
        print(f"Available: {', '.join(IMAGES)}")
        return 1

    shutil.rmtree(WORK_ROOT, ignore_errors=True)
    clear_dump_dir()

    results = TestResult()
    for image in images:
        try:
            run_image_tests(results, image)
        except Exception as e:  # noqa: BLE001 - keep going after a crash
            results.check(image, f"unhandled exception in the tests for this image",
                          False, f"{type(e).__name__}: {e}")

    # If every image's working directory has already been cleaned up, remove
    # the (now empty) top-level directory as well:
    if os.path.isdir(WORK_ROOT) and not os.listdir(WORK_ROOT):
        shutil.rmtree(WORK_ROOT, ignore_errors=True)

    print("\n====================================================================")
    print(f"Summary: {results.passed} passed, {results.failed} failed "
          f"out of {results.passed + results.failed} checks")
    if results.failures:
        print("\nFailed checks:")
        for image, name, detail in results.failures:
            print(f"  - {image} / {name}")
        return 1
    print("All tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
