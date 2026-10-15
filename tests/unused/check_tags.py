import struct

IMG = "/tmp/50MB_blank.image"

def interleave5(sector):
    d = (0, 4, 8, 12, 0, 4, 8, -4, 0, 4, -8, -4, 0, -12, -8, -4)
    return sector + d[sector & 15]

data = open(IMG, 'rb').read()
num_sectors = len(data) // 532

bad = []
no_checksum_bit = 0
for sec in range(num_sectors):
    pos = interleave5(sec) * 532
    tag = data[pos:pos+20]
    sector_data = data[pos+20:pos+532]
    dataused = struct.unpack('>H', tag[6:8])[0]
    if not (dataused & 0x8000):
        no_checksum_bit += 1
        continue
    csum = 0
    for b in sector_data:
        csum ^= b
    for b in tag:
        csum ^= b
    if csum != 0:
        file_id = struct.unpack('>H', tag[4:6])[0]
        bad.append((sec, csum, file_id, dataused))

print(f"sectors checked: {num_sectors}; tags without 0x8000 checksum bit: {no_checksum_bit}")
print(f"sectors with CHECKSUM MISMATCH: {len(bad)}")
for sec, csum, fid, du in bad:
    print(f"  sector {sec}: csum=0x{csum:02X} file_id=0x{fid:04X} dataused=0x{du:04X}")
