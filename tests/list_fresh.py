import struct
IMG = "/tmp/50MB_LOS1.0andPascalWorkshop1.0.image"
def interleave5(sector):
    d = (0, 4, 8, 12, 0, 4, 8, -4, 0, 4, -8, -4, 0, -12, -8, -4)
    return sector + d[sector & 15]
data = open(IMG,'rb').read()
num = len(data)//532
mddf = 38
def sec_data(sec): return data[interleave5(sec)*532+20 : interleave5(sec)*532+20+512]
mddf_bytes = sec_data(mddf)
slist_addr = struct.unpack('>I', mddf_bytes[0x94:0x98])[0]
slist_packing = struct.unpack('>H', mddf_bytes[0x98:0x9a])[0]
empty_file = struct.unpack('>H', mddf_bytes[0x9e:0xa0])[0]
filecount = struct.unpack('>H', mddf_bytes[0xb0:0xb2])[0]
print(f"empty_file={empty_file} filecount={filecount}")
for s in range(1, max(empty_file, filecount)+1):
    page = mddf + slist_addr + s//slist_packing
    off = (s % slist_packing)*14
    tb = sec_data(page)
    hintaddr = struct.unpack('>I', tb[off:off+4])[0]
    if hintaddr == 0: continue
    h = sec_data(mddf + hintaddr)
    name = h[1:1+h[0]]
    try: name = name.decode('mac-roman')
    except: name = repr(name)
    mid = int.from_bytes(h[0x42:0x46],'big')
    prot = h[0x48]
    print(f"sfile {s:4d}: '{name}' hint={mddf+hintaddr} mid={mid:#010x} prot={prot}")
