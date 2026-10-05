#!/usr/bin/env python3
"""
Fix invalid flat-catalog centry types in LISA_BASIC_2.0 disk images.

Root cause of Workshop's "Invalid s-file number" (E_SNUM_RANGE / 835):
  The BASIC 2.0 disks' root catalogs contain centries whose cetype byte is
  0x20 (32) -- an INVALID entry type (valid range is 0..8:
  emptyentry=0, direntry=1, linkentry=2, fileentry=3, pipeentry=4,
  ecentry=5, killedentry=6, removed=7, threadentry=8).
  These slots are all-space, so their sfile field reads 0x2020 = 8224,
  which is far beyond maxfiles (107). The file manager's directory
  enumeration treats any non-empty slot as a live file and calls
  slist_io(8224) -> E_SNUM_RANGE -> "Invalid s-file number".

  The working Pascal Workshop disks have ZERO cetype=32 slots, and their
  removed(7) tombstones carry sfile=0. The BASIC disks' removed(7)
  tombstones are also all-space (sfile=8224).

Fix (per disk):
  1. Every centry with cetype==32 (0x20) is rewritten as a proper
     removed(7) tombstone: the whole 54-byte centry is filled with 0x00
     except cetype byte (offset 34) = 7. Using removed (not emptyentry)
     keeps LOOKUP_BY_ENAME's forward scan from stopping early, because
     these slots are interleaved between live fileentries.
  2. Every remaining non-fileentry centry (emptyentry/removed/etc.) gets
     its sfield (offsets 36-37) forced to 0, so no out-of-range sfile
     can ever be resolved.
  3. Fileentries (cetype==3) are left completely untouched.
  4. The DC42 data checksum (header offset 0x48) is recomputed.

Only the 12 rootcatalog sectors (35..46) are modified.
"""
import struct
import sys

SECTOR = 512
HDR = 84
CAT_SECTORS = list(range(35, 47))   # rootcatalog data sectors
CAT_SIZE = 5778                     # 107 centries * 54 bytes
CENTRY = 54
CET_OFF = 34
SF_OFF = (36, 38)
EMPTY = 0
FILEENTRY = 3
REMOVED = 7
INVALID = 32


def dc42_data_checksum(data: bytes) -> int:
    """DC42 checksum, identical to LisaFileSystemTool.compute_dc42_checksum:
    add each 16-bit big-endian word into a 32-bit accumulator and rotate
    right one bit after each add."""
    def addl_rorl(uint, csum):
        csum += uint
        csum &= 0xFFFFFFFF
        rbit = csum & 0x1
        csum >>= 1
        csum += rbit << 31
        return csum
    checksum = 0
    for i in range(0, len(data), 2):
        word = struct.unpack(">H", data[i:i + 2])[0]
        checksum = addl_rorl(word, checksum)
    return checksum


def fix_disk(src: str, dst: str) -> None:
    img = bytearray(open(src, "rb").read())
    nsectors = (len(img) - HDR) // (SECTOR + 12)  # data + 12-byte tag each
    data_end = HDR + nsectors * SECTOR
    data = img[HDR:data_end]

    # --- rebuild the rootcatalog ---
    cat = bytearray()
    for n in CAT_SECTORS:
        cat += data[n * SECTOR:(n + 1) * SECTOR]
    # Keep the full 12 sectors (6144 bytes). Only the first CAT_SIZE bytes
    # (107 centries) are the real catalog; the tail of the last sector is
    # padding and must be preserved byte-for-byte.
    assert len(cat) == len(CAT_SECTORS) * SECTOR

    n_fixed_invalid = 0
    n_fixed_sfile = 0
    for i in range(CAT_SIZE // CENTRY):
        off = i * CENTRY
        c = cat[off:off + CENTRY]
        ct = c[CET_OFF]
        if ct == FILEENTRY:
            continue  # leave live files alone
        if ct == INVALID:
            # rewrite as a clean removed(7) tombstone
            cat[off:off + CENTRY] = b"\x00" * CENTRY
            cat[off + CET_OFF] = REMOVED
            n_fixed_invalid += 1
        # force sfile to 0 on every non-fileentry slot
        sf = struct.unpack(">H", c[SF_OFF[0]:SF_OFF[1]])[0]
        if sf != 0:
            cat[off + SF_OFF[0]:off + SF_OFF[1]] = struct.pack(">H", 0)
            n_fixed_sfile += 1

    # --- write the rebuilt catalog back into the sectors ---
    for idx, n in enumerate(CAT_SECTORS):
        chunk = cat[idx * SECTOR:(idx + 1) * SECTOR]
        data[n * SECTOR:(n + 1) * SECTOR] = chunk

    # --- recompute the DC42 data checksum ---
    img[HDR:data_end] = data
    newsum = dc42_data_checksum(bytes(data))
    img[0x48:0x4C] = struct.pack(">I", newsum)

    open(dst, "wb").write(bytes(img))
    print(f"{src} -> {dst}")
    print(f"  cetype=32 slots converted to removed(7): {n_fixed_invalid}")
    print(f"  non-fileentry slots with sfile forced to 0: {n_fixed_sfile}")
    print(f"  new DC42 data checksum: 0x{newsum:08x}")


if __name__ == "__main__":
    pairs = [
        ("LISA_BASIC_2.0_disk1.dc42", "LISA_BASIC_2.0_disk1_fixed.dc42"),
        ("LISA_BASIC_2.0_disk2.dc42", "LISA_BASIC_2.0_disk2_fixed.dc42"),
        ("LISA_BASIC_2.0_disk3.dc42", "LISA_BASIC_2.0_disk3_fixed.dc42"),
        ("LISA_BASIC_2.0_disk4.dc42", "LISA_BASIC_2.0_disk4_fixed.dc42"),
    ]
    for s, d in pairs:
        try:
            fix_disk(s, d)
        except FileNotFoundError:
            print(f"  (skipped, not found: {s})")
