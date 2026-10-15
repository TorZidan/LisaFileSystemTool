import struct, sys
IMG = sys.argv[1]
def interleave5(sector):
    d = (0, 4, 8, 12, 0, 4, 8, -4, 0, 4, -8, -4, 0, -12, -8, -4)
    return sector + d[sector & 15]
data = open(IMG,'rb').read()
num = len(data)//532
bad = []
for sec in range(num):
    pos = interleave5(sec)*532
    tag = data[pos:pos+20]
    d = data[pos+20:pos+532]
    if not (struct.unpack('>H', tag[6:8])[0] & 0x8000):
        continue
    c = 0
    for b in d: c ^= b
    for b in tag: c ^= b
    if c != 0:
        bad.append((sec, c, struct.unpack('>H', tag[4:6])[0]))
print(f"{IMG}: {len(bad)} failing sectors")
for sec, c, fid in bad:
    print(f"  sector {sec}: csum=0x{c:02X} file_id=0x{fid:04X}")
