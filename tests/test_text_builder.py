#!/usr/bin/env python3
"""Unit tests for build_lisa_text_file_data() per LisaOsTextFileSpecification.txt."""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from LisaFileSystemToolPerFile import (
    build_lisa_text_file_data,
    TEXT_FILE_PAGE_SIZE,
    TEXT_FILE_HEADER_SIZE,
)

PAGE = TEXT_FILE_PAGE_SIZE
CR = 0x0D
failures = 0


def check(name, cond, detail=""):
    global failures
    if cond:
        print(f"  ok: {name}")
    else:
        failures += 1
        print(f"  FAIL: {name} {detail}")


def verify_structure(out, label):
    """Verify the spec invariants of the whole on-disk byte stream."""
    check(f"[{label}] len is a whole number of pages", len(out) % PAGE == 0 and len(out) >= PAGE, f"len={len(out)}")
    check(f"[{label}] header page all nulls", out[:PAGE] == bytes(PAGE))
    data = out[PAGE:]
    pages = [data[i : i + PAGE] for i in range(0, len(data), PAGE)]
    for pi, page in enumerate(pages):
        pos = page.find(b"\r\x00")
        check(f"[{label}] page {pi}: has a CR-null terminator", 0 <= pos <= PAGE - 2, f"pos={pos}")
        if pos < 0:
            continue
        check(f"[{label}] page {pi}: nulls after the last line", all(b == 0 for b in page[pos + 1 :]))
        # every line in the page is complete: each CR before the terminator is
        # followed by a non-null (the first char of the next line)
        ok = all(page[p + 1] != 0 for p in range(pos) if page[p] == CR)
        check(f"[{label}] page {pi}: all lines CR-terminated", ok)
        # every line is at most 1023 bytes counting its CR
        lines, start = [], 0
        for p in range(pos + 1):
            if page[p] == CR:
                lines.append(p - start + 1)
                start = p + 1
        check(f"[{label}] page {pi}: lines <= 1023 bytes incl CR", all(1 <= L <= PAGE - 1 for L in lines), f"lines={lines}")
    # the text data ends with a CR (the terminator of the last page)
    last = pages[-1]
    lpos = last.find(b"\r\x00")
    check(f"[{label}] text data ends with CR", last[lpos] == CR)
    return pages


def parse_pages(pages):
    """Re-parse like a Lisa reader: per page, content up to (incl.) the first CR of the CR-null pair."""
    out = bytearray()
    for page in pages:
        pos = page.find(b"\r\x00")
        out += page[: pos + 1]
    return bytes(out)


print("== case 1: empty content -> null text file (header page only) ==")
out = build_lisa_text_file_data(b"")
check("len == 1024", len(out) == PAGE)
check("all nulls", out == bytes(PAGE))

print("== case 2: simple text, no trailing newline ==")
out = build_lisa_text_file_data(b"Hello")
pages = verify_structure(out, "hello")
check("one text page", len(pages) == 1)
check("content is Hello\\r", pages[0][:6] == b"Hello\r")
check("reparse", parse_pages(pages) == b"Hello\r")

print("== case 3: line-ending normalization (\\n, \\r\\n, \\r give identical results) ==")
a = build_lisa_text_file_data(b"one\ntwo\nthree\n")
b = build_lisa_text_file_data(b"one\r\ntwo\r\nthree\r\n")
c = build_lisa_text_file_data(b"one\rtwo\rthree\r")
check("LF == CRLF", a == b)
check("LF == CR", a == c)
pages = verify_structure(a, "norm")
check("reparse", parse_pages(pages) == b"one\rtwo\rthree\r")

print("== case 4: trailing newline already present ==")
out = build_lisa_text_file_data(b"abc\r\n")
pages = verify_structure(out, "trail")
check("reparse", parse_pages(pages) == b"abc\r")

print("== case 5: long single line (2500 chars) spans 3 pages ==")
long_line = b"A" * 2500
out = build_lisa_text_file_data(long_line)
pages = verify_structure(out, "long")
check("3 text pages", len(pages) == 3)
check("page1 = 1022 A + CR + NUL", pages[0] == b"A" * 1022 + b"\r\x00")
check("page2 = 1022 A + CR + NUL", pages[1] == b"A" * 1022 + b"\r\x00")
check("page3 = 456 A + CR + nulls", pages[2][:457] == b"A" * 456 + b"\r" and pages[2][457:] == bytes(PAGE - 457))
# the original line is recoverable by stripping the inserted boundary CRs:
recovered = parse_pages(pages).replace(b"A\rA", b"AA")  # undo boundary splits
check("line recoverable", recovered == b"A" * 2500 + b"\r", f"len={len(recovered)}")

print("== case 6: line of exactly 1023 bytes (1022 chars + CR) fits one page ==")
out = build_lisa_text_file_data(b"B" * 1022 + b"\r")
pages = verify_structure(out, "exact")
check("one text page", len(pages) == 1)
check("page = line + 1 NUL", pages[0] == b"B" * 1022 + b"\r\x00")

print("== case 7: 1023 chars without CR -> CR appended, line split at boundary ==")
out = build_lisa_text_file_data(b"C" * 1023)
pages = verify_structure(out, "split")
check("two text pages", len(pages) == 2)
check("page1 = 1022 C + CR + NUL", pages[0] == b"C" * 1022 + b"\r\x00")
check("page2 = 1 C + CR + nulls", pages[1][:2] == b"C\r" and pages[1][2:] == bytes(PAGE - 2))

print("== case 8: a line that does not fit in the space left on a page ==")
# is moved whole to the next page (like the real Lisa editor): the previous
# page stays short, and the line is NOT split at the page boundary.
text = (b"D" * 100 + b"\r") * 10  # 10 lines x 101 bytes = 1010 bytes, all in page 1
text += b"E" * 2000 + b"\r"        # one long line (2001 bytes) that does not fit in the 13 bytes left
out = build_lisa_text_file_data(text)
pages = verify_structure(out, "mixed")

check("page1 = the 10 short lines + null padding (short page)", pages[0][:1010] == (b"D" * 100 + b"\r") * 10 and pages[0][1010:] == bytes(PAGE - 1010))
check("page1 holds no E's (the long line was moved whole)", b"E" not in pages[0][:1011])
check("page2 = 1022 E + CR + NUL", pages[1][:1023] == b"E" * 1022 + b"\r")
check("3 text pages total", len(pages) == 3, f"got {len(pages)}")
rest = 2001 - 1022  # bytes of the long line left for page3, incl. its final CR = 979
check("page3 = 978 E + CR + nulls", pages[2][:rest] == b"E" * (rest - 1) + b"\r" and pages[2][rest:] == bytes(PAGE - rest), f"rest={rest}")

print("== case 9: only newlines (empty lines) ==")
out = build_lisa_text_file_data(b"\n\n\n")
pages = verify_structure(out, "emptylines")
check("reparse", parse_pages(pages) == b"\r\r\r")

print("== case 10: arbitrary bytes (NULs in the middle of a line) ==")
content = b"ab\x00cd\r"
out = build_lisa_text_file_data(content)
pages = verify_structure(out, "nul")
check("NUL kept as a character", parse_pages(pages) == b"ab\x00cd\r")

print("== case 11: regression — a line crossing the old 1023-byte window is not split ==")
# (the EDIT.MENUS.TEXT 'open' -> 'o'+'pen' bug: the line must stay intact)
from LisaFileSystemTool import lisa_text_file_to_host_text
host = b"A" * 1019 + b"\n" + b"open\nclose\n"  # 'open' starts at byte 1020 of the CR-normalized text
out = build_lisa_text_file_data(host)
pages = verify_structure(out, "nosp")
check("two text pages", len(pages) == 2, f"got {len(pages)}")
check("page1 = the A-line + null padding", pages[0][:1020] == b"A" * 1019 + b"\r" and pages[0][1020:] == bytes(PAGE - 1020))
check("page2 lines intact", parse_pages(pages) == b"A" * 1019 + b"\r" + b"open\r" + b"close\r")
check("dump->add->dump round trip is exact",
      lisa_text_file_to_host_text(build_lisa_text_file_data(host)) == host)

print()
if failures:
    print(f"{failures} FAILURE(S)")
    sys.exit(1)
print("All builder tests passed.")
