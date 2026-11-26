#!/usr/bin/env python3
"""List all hint sectors (tag fileid = -s_file_id, relpage low 2 bytes == 0) of a
raw ProFile image, with hentry name, machine_id, protected flag, and checksum state.
Also resolves s_file_id from the tag and shows the slist entry if MDDF found."""
import struct, sys

DELTA = (0, 4, 8, 12, 0, 4, 8, -4, 0, 4, -8, -4, 0, -12, -8, -4)

def pascal(b, start=0):
    n = b[start]
    if n > 33: return f"<bad len {n}>"
    return b[start+1:start+1+n].decode('cp1252', errors='replace')

def main(path):
    with open(path, 'rb') as f:
        data = f.read()
    nblocks = len(data) // 532
    print(f"== {path}: {nblocks} sectors ==")
    for phys in range(nblocks):
        off = phys * 532
        tag = data[off:off+20]
        sec = data[off+20:off+532]
        fid = struct.unpack('>H', tag[4:6])[0]
        if fid <= 0x7FFF:
            continue  # not a hint sector (hint sectors have negative fileid = 0x8000..0xFFFF)
        rel2 = (tag[12] << 8) | tag[13]
        if rel2 != 0:
            continue
        sfile = (0x10000 - fid) % 0x10000
        c = 0
        for b in tag: c ^= b
        for b in sec: c ^= b
        name = pascal(sec, 0)
        mid = struct.unpack('>I', sec[0x42:0x46])[0]
        prot = sec[0x48]
        master = sec[0x49]
        dataused = struct.unpack('>H', tag[6:8])[0]
        print(f"  phys {phys:6}: sfile {sfile:4}: name='{name}' machine_id={mid:#010x} "
              f"protected={prot} master={master} dataused={dataused & 0x7FFF:4} "
              f"chk={'OK ' if c == 0 else 'BAD(%#04x)' % c} tag={' '.join(f'{b:02x}' for b in tag)}")

if __name__ == '__main__':
    main(sys.argv[1])
