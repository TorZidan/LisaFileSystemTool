#!/usr/bin/env python3
"""Inspect specific physical sectors of a raw ProFile image: tag + hentry fields."""
import struct, sys

DELTA = (0, 4, 8, 12, 0, 4, 8, -4, 0, 4, -8, -4, 0, -12, -8, -4)

def pascal(b, start=0):
    n = b[start]
    return b[start+1:start+1+n].decode('cp1252', errors='replace')

def main(path, *phys_list):
    with open(path, 'rb') as f:
        data = f.read()
    nblocks = len(data) // 532
    print(f"{path}: {nblocks} sectors")
    for phys in phys_list:
        off = phys * 532
        tag = data[off:off+20]
        sec = data[off+20:off+532]
        ver, volid, fid = struct.unpack('>HHH', tag[0:6])
        dataused = struct.unpack('>H', tag[6:8])[0]
        abs3 = (tag[8] << 16) | (tag[9] << 8) | tag[10]
        chk = tag[11]
        rel2 = (tag[12] << 8) | tag[13]
        fwd = (tag[14] << 16) | (tag[15] << 8) | tag[16]
        bkwd = (tag[17] << 16) | (tag[18] << 8) | tag[19]
        print(f"\nphys {phys}: tag = {' '.join(f'{b:02x}' for b in tag)}")
        print(f"   ver={ver:#06x} volid={volid:#06x} fid={fid:#06x} dataused={dataused:#06x} abs3={abs3:#08x} chk={chk:#04x} rel2={rel2:#06x} fwd={fwd:#08x} bkwd={bkwd:#08x}")
        c = 0
        for b in tag: c ^= b
        for b in sec: c ^= b
        print(f"   checksum rule result: {'PASS' if c == 0 else 'FAIL (XOR=%#04x)' % c}")
        name = pascal(sec, 0)
        print(f"   hentry name: '{name}'")
        uid_a = struct.unpack('>I', sec[0x22:0x26])[0]
        uid_b = struct.unpack('>I', sec[0x26:0x2a])[0]
        ver_f = struct.unpack('>H', sec[0x2a:0x2c])[0]
        ftype = sec[0x2c]
        mid = struct.unpack('>I', sec[0x42:0x46])[0]
        flags = {n: sec[0x46+i] for i, n in enumerate(
            ["killed","safety_on","protected","master","scavenged","closed_by_OS","file_open"])}
        print(f"   uid=({uid_a:#010x},{uid_b:#010x}) version={ver_f} ftype={ftype}")
        print(f"   machine_id={mid} ({mid:#010x})")
        print(f"   flags: {flags}")

if __name__ == '__main__':
    main(sys.argv[1], *[int(x, 0) for x in sys.argv[2:]])
