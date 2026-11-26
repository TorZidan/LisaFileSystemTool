#!/usr/bin/env python3
"""Synthetic end-to-end test of the summary line in AnalyzeLisaExecutableFile.analyze()."""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from AnalyzeLisaExecutableFile import LisaExecutableFileAnalyzer
from LisaFileSystemToolPerFile import _LocatedFile

DESC = b"\x00\x04\x00\x00\x00\x04\x7f\xff"  # 8-byte segment descriptor

def build_obj():
    obj = b"\x9b\x00" + b"\x00\x00" + struct.pack(">H", 1) + struct.pack(">H", 1)
    obj += b"PASLIB  " + struct.pack(">H", 1) + struct.pack(">H", 1)
    obj += b"\x00" * 18  # padding so the unit entry at 8 does not swallow the segment run
    obj += b"PASLIB1 " + struct.pack(">H", 17) + DESC
    obj += b"PASIOLIB" + struct.pack(">H", 3) + DESC
    return obj

def build_lib(paslib1_desc=DESC):
    lib = b"PASLIB  " + struct.pack(">H", 1) + b"\x01\x00" + b"\x00\x00\x00\x00"
    lib += b"PASIOLIB" + struct.pack(">H", 3) + b"\x01\x00" + b"\x00\x00\x00\x00"
    lib += b"\x9c\x00" + b"\x00\x00\x00\x00"  # dir starts at marker+6
    lib += b"PASLIB1 " + struct.pack(">H", 17) + paslib1_desc + b"\x00\x01" + b"\x00" * 8
    lib += b"PASIOLIB" + struct.pack(">H", 3) + DESC + b"\x00\x01" + b"\x00" * 8
    # 9e 00 table: 1 entry, lib_file_id 1 -> Pascal string at offset 106
    lib += b"\x9e\x00" + struct.pack(">H", 0) + struct.pack(">H", 1)
    lib += struct.pack(">H", 1) + struct.pack(">I", 106)
    lib += b"\x0d" + b"IOSPASLIB.OBJ"
    return lib

def run_case(label, obj_bytes, lib_bytes, expect):
    a = LisaExecutableFileAnalyzer.__new__(LisaExecutableFileAnalyzer)
    a.read_file_from_disk_image = lambda name: obj_bytes if name == "BASIC.OBJ" else lib_bytes
    a.file_exists_on_volume = lambda name: True
    a._locate_named_file = lambda name: _LocatedFile(status="file", name="BASIC.OBJ")
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        a.analyze("BASIC.OBJ")
    out = buf.getvalue()
    ok = expect in out
    print(f"[{label}] {'PASS' if ok else 'FAIL'} (expected: {expect!r})")
    if not ok:
        print(out)
    return ok

obj = build_obj()
ok1 = run_case("all-OK", obj, build_lib(),
               "Everything matches and all intrinsic library files needed to execute file 'BASIC.OBJ' are present on the disk image: IOSPASLIB.OBJ.")
ok2 = run_case("seg-mismatch", obj, build_lib(paslib1_desc=b"\x00\x04\xff\x00\x00\x04\x7f\xff"),
               "!!!!! PROBLEMS FOUND !!!!! :")
print("ALL PASS" if (ok1 and ok2) else "SOME FAILED")
sys.exit(0 if (ok1 and ok2) else 1)
