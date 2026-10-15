"""Simulate the Lisa OS marking files as protected: set hentry machine_id + protected flag,
then fix the tag checksum byte so the sector is valid (as the OS would have written it)."""
import struct

path = "/tmp/v14test.image"
data = bytearray(open(path, 'rb').read())
nblocks = len(data) // 532

def tag_off(rec): return rec * 532
def data_off(rec): return rec * 532 + 20

def find_hint_rec(sfile):
    expected = (0x10000 - sfile) % 0x10000
    for rec in range(nblocks):
        t = data[tag_off(rec):tag_off(rec)+20]
        if struct.unpack('>H', t[4:6])[0] == expected:
            if struct.unpack('>H', t[12:14])[0] == 0:
                return rec
    return None

def fix_chk(rec):
    off = tag_off(rec)
    c = 0
    for i in range(20):
        if i != 11: c ^= data[off+i]
    for b in data[data_off(rec):data_off(rec)+512]:
        c ^= b
    data[off+11] = c

targets = {7: 0x04212345, 8: 0x09876543, 9: 0x12345678, 10: 0x55500001}
for sfile, mid in targets.items():
    rec = find_hint_rec(sfile)
    assert rec is not None, f"no hint for sfile {sfile}"
    abssec = 38 + struct.unpack('>H', data[tag_off(rec)+12:tag_off(rec)+14])[0]
    d = data_off(rec)
    name = data[d+1:d+1+data[d]]
    data[d+0x42:d+0x46] = mid.to_bytes(4, 'big')
    data[d+0x48] = 1
    fix_chk(rec)
    print(f"sfile {sfile:3}: '{name.decode('mac-roman','replace')}' hint at record {rec} (abs sec {abssec}): set machine_id={mid:#010x}, protected=1, chk fixed")

open(path, 'wb').write(data)
print("done")
