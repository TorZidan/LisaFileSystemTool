import struct
IMG = "/tmp/50MB_blank.image"
def interleave5(sector):
    d = (0, 4, 8, 12, 0, 4, 8, -4, 0, 4, -8, -4, 0, -12, -8, -4)
    return sector + d[sector & 15]
data = open(IMG,'rb').read()
for sec in range(len(data)//532):
    pos = interleave5(sec)*532
    tag = data[pos:pos+20]
    du = struct.unpack('>H', tag[6:8])[0]
    if not (du & 0x8000):
        fid = struct.unpack('>H', tag[4:6])[0]
        print(f"sector {sec}: dataused={du:#06x} (NO checksum bit) file_id={fid:#06x} tag={tag.hex(' ')}")
