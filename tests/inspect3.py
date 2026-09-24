import struct
IMG = "/tmp/50MB_blank.image"
def interleave5(sector):
    d = (0, 4, 8, 12, 0, 4, 8, -4, 0, 4, -8, -4, 0, -12, -8, -4)
    return sector + d[sector & 15]
data = open(IMG,'rb').read()
for sec in (280, 660, 2416):
    pos = interleave5(sec)*532
    tag = data[pos:pos+20]
    d = data[pos+20:pos+532]
    print(f"--- sector {sec} ---")
    print("tag hex:", tag.hex(' '))
    fid = struct.unpack('>H', tag[4:6])[0]
    print(f"file_id={fid:#06x} (sfile {0x10000-fid if fid>0x8000 else fid}), dataused={struct.unpack('>H',tag[6:8])[0]:#06x}, abs={int.from_bytes(tag[8:11],'big')}")
    print(f"checksum byte11={tag[11]:#04x}, rel={int.from_bytes(tag[12:14],'big')}, fwd={int.from_bytes(tag[14:17],'big')}, bkwd={int.from_bytes(tag[17:20],'big')}")
    c=0
    for b in d: c^=b
    for b in tag: c^=b
    print(f"running csum (incl. byte11): {c:#04x}; needed byte11 = {c ^ tag[11]:#04x}")
    # hentry fields
    name_len = d[0]
    print("name:", d[1:1+name_len])
    print(f"machine_id@0x42={int.from_bytes(d[0x42:0x46],'big'):#010x}, flags@0x46..0x4C={d[0x46:0x4D].hex(' ')}")
