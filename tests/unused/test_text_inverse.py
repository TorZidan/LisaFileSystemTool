#!/usr/bin/env python3
"""Test lisa_text_file_to_host_text() as the inverse of build_lisa_text_file_data()."""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from LisaFileSystemToolPerFile import build_lisa_text_file_data
from LisaFileSystemTool import lisa_text_file_to_host_text

failures = 0


def check(name, cond, detail=""):
    global failures
    if cond:
        print(f"  ok: {name}")
    else:
        failures += 1
        print(f"  FAIL: {name} {detail}")


def roundtrip(name, host_bytes):
    """build -> disk layout -> host conversion; compare with the normalized expectation."""
    on_disk = build_lisa_text_file_data(host_bytes)
    back = lisa_text_file_to_host_text(on_disk)
    expect = host_bytes.replace(b"\r\n", b"\r").replace(b"\n", b"\r")
    if expect and not expect.endswith(b"\r"):
        expect += b"\r"
    expect = expect.replace(b"\r", b"\n")
    check(name, back == expect, f"got {back[:80]!r}... expected {expect[:80]!r}...")
    return back


print("== exact inverse for ordinary text (no lines > 1022 chars) ==")
roundtrip("simple LF", b"Hello\nworld\n")
roundtrip("CRLF", b"Hello\r\nworld\r\n")
roundtrip("CR already", b"Hello\rworld\r")
roundtrip("no trailing newline", b"Hello world")
roundtrip("empty", b"")
# 31-byte lines x 33 fill each 1023-byte page window exactly (1023 = 33 x 31),
# so no line crosses a page boundary and the round trip is exact:
roundtrip("many lines (2 pages)", (b"x" * 30 + b"\n") * 66)
roundtrip("NUL mid-line kept", b"ab\x00cd\r")
roundtrip("only newlines", b"\n\n\n")

print("== long lines: inserted boundary CRs appear as extra \\n ==")
long_line = b"A" * 2500  # split into 1022/1022/456 across 3 pages: 2 inserted CRs
back = lisa_text_file_to_host_text(build_lisa_text_file_data(long_line))
check(
    "long line = AAA...A\\nAAA...A\\nAAA...A\\n (2 boundary splits)",
    back == b"A" * 1022 + b"\n" + b"A" * 1022 + b"\n" + b"A" * 456 + b"\n",
    f"len={len(back)}, newline count={back.count(b'\n')}",
)

print("== direct property tests on lisa_text_file_to_host_text ==")
page = 1024
# header + one page: "Hi\r" + nulls
check("null text file -> empty", lisa_text_file_to_host_text(bytes(page)) == b"")
check(
    "one page",
    lisa_text_file_to_host_text(bytes(page) + b"Hi\r" + bytes(page - 3)) == b"Hi\n",
)
# two pages, second page fully used except final CR
p1 = b"x" * 100 + b"\r" + bytes(page - 101)
p2 = b"y" * 1022 + b"\r" + bytes(1)
check("two pages", lisa_text_file_to_host_text(bytes(page) + p1 + p2) == b"x" * 100 + b"\n" + b"y" * 1022 + b"\n")
# malformed page without CR-null: trailing nulls dropped, data kept
check(
    "malformed page (no CR-null)",
    lisa_text_file_to_host_text(bytes(page) + b"abc" + bytes(page - 3)) == b"abc",
)
# malformed whole file shorter than a page: kept whole
check("short malformed file", lisa_text_file_to_host_text(b"zz") == b"zz")

print("== DLE leading-space compression (optional per the spec) ==")
# A line may start with DLE (0x10) + (32 + N) representing N literal spaces.
check(
    "DLE code at line start expanded",
    lisa_text_file_to_host_text(bytes(page) + b"\x10" + bytes([32 + 5]) + b"abc\r" + bytes(page - 8))
    == b"     abc\n",
)
check(
    "DLE code of one space",
    lisa_text_file_to_host_text(bytes(page) + b"\x10" + bytes([33]) + b"x\r" + bytes(page - 6))
    == b" x\n",
)
check(
    "line consisting only of a DLE code",
    lisa_text_file_to_host_text(bytes(page) + b"\x10" + bytes([32 + 12]) + b"\r" + bytes(page - 5))
    == b" " * 12 + b"\n",
)
check(
    "DLE codes on several lines",
    lisa_text_file_to_host_text(
        bytes(page)
        + b"\x10" + bytes([32 + 2]) + b"a\r\x10" + bytes([32 + 9]) + b"b\r\r"
        + bytes(page - 9)
    )
    == b"  a\n         b\n\n",
)
check(
    "DLE in the middle of a line is kept as-is",
    lisa_text_file_to_host_text(bytes(page) + b"ab\x10" + bytes([32 + 3]) + b"cd\r" + bytes(page - 8))
    == b"ab\x10" + bytes([35]) + b"cd\n",
)
check(
    "degenerate code (DLE + 32 = zero spaces) is kept as-is",
    lisa_text_file_to_host_text(bytes(page) + b"\x10" + bytes([32]) + b"cd\r" + bytes(page - 7))
    == b"\x10" + bytes([32]) + b"cd\n",
)
check(
    "single trailing DLE byte at line start is kept",
    lisa_text_file_to_host_text(bytes(page) + b"\x10\r" + bytes(page - 3))
    == b"\x10\n",
)
# a DLE line whose CR-null is the last byte pair of page 1: the code must be
# expanded per line even when the next line starts the next page
p1dle = b"z\r" * 510 + b"\x10" + bytes([32 + 7]) + b"\r" + bytes(1)
p2dle = b"plain\r" + bytes(page - 6)
check(
    "DLE code at a page boundary",
    lisa_text_file_to_host_text(bytes(page) + p1dle + p2dle)
    == b"z\n" * 510 + b" " * 7 + b"\nplain\n",
)

print()
if failures:
    print(f"{failures} FAILURE(S)")
    sys.exit(1)
print("All inverse-function tests passed.")
