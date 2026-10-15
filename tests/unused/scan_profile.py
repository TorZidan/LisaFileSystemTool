#!/usr/bin/env python3
"""Scan a raw ProFile image (20-byte tag + 512 data per sector, interleaved) and
report sectors whose per-sector tag checksum (byte 11) does not satisfy the
HDISK/PROFASM read-verify rule:

    XOR(all 20 tag bytes) ^ XOR(all 512 data bytes) == 0     (when flag bit set)

Also prints, for each failing sector, the logical sector number, tag fields,
and what the checksum byte *should* be.
"""
import struct
import sys

DELTA = (0, 4, 8, 12, 0, 4, 8, -4, 0, 4, -8, -4, 0, -12, -8, -4)

def interleave5(sector: int) -> int:
    return sector + DELTA[sector & 15]

def main(path):
    with open(path, 'rb') as f:
        data = f.read()
    nblocks = len(data) // 532
    print(f"{path}: {len(data)} bytes = {nblocks} sectors of 532")

    bad = []
    for phys in range(nblocks):
        off = phys * 532
        tag = data[off:off+20]
        sec = data[off+20:off+532]
        c = 0
        for b in tag: c ^= b
        for b in sec: c ^= b
        if c != 0:
            bad.append((phys, c, tag))

    print(f"sectors failing tag-checksum rule: {len(bad)}")
    for phys, c, tag in bad:
        # invert interleave: logical L satisfies interleave5(L) == phys
        L = phys
        # search small window
        for cand in range(phys - 20, phys + 20):
            if cand >= 0 and interleave5(cand) == phys:
                L = cand
                break
        ver, volid, fid = struct.unpack('>HHH', tag[0:6])
        dataused = struct.unpack('>H', tag[6:8])[0]
        abs3 = (tag[8] << 16) | (tag[9] << 8) | tag[10]
        chk = tag[11]
        rel2 = (tag[12] << 8) | tag[13]
        fwd = (tag[14] << 16) | (tag[15] << 8) | tag[16]
        bkwd = (tag[17] << 16) | (tag[18] << 8) | tag[19]
        # what checksum byte would make it pass?
        x = 0
        for b in tag[0:11] + tag[12:20]: x ^= b
        off = phys * 532
        sec = data[off+20:off+532]
        for b in sec: x ^= b
        print(f"  phys {phys:6} (logical {L:6}): fail XOR={c:#04x} "
              f"ver={ver:#06x} volid={volid:#06x} fid={fid:#06x} dataused={dataused:#06x} "
              f"abs3={abs3:#08x} chk={chk:#04x} rel2={rel2:#06x} fwd={fwd:#08x} bkwd={bkwd:#08x} "
              f"| correct chk would be {x:#04x}")

if __name__ == '__main__':
    main(sys.argv[1])
