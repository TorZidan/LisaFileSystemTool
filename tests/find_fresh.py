import struct
IMG = "/tmp/50MB_LOS1.0andPascalWorkshop1.0.image"
def interleave5(sector):
    d = (0, 4, 8, 12, 0, 4, 8, -4, 0, 4, -8, -4, 0, -12, -8, -4)
    return sector + d[sector & 15]
data = open(IMG,'rb').read()
num = len(data)//532
print("size", len(data), "sectors", num)
# find MDDF: scan tags for file_id 0x0001
mddf = None
for sec in range(num):
    pos = interleave5(sec)*532
    tag = data[pos:pos+20]
    fid = struct.unpack('>H', tag[4:6])[0]
    if fid == 0x0001:
        mddf = sec
        break
print("MDDF sector:", mddf)
mddf_bytes = data[interleave5(mddf)*532+20 : interleave5(mddf)*532+20+512]
print("fs version:", struct.unpack('>H', mddf_bytes[0:2])[0])
slist_addr = struct.unpack('>I', mddf_bytes[0x94:0x98])[0]
slist_packing = struct.unpack('>H', mddf_bytes[0x98:0x9a])[0]
slist_count = struct.unpack('>H', mddf_bytes[0x9a:0x9c])[0]
empty_file = struct.unpack('>H', mddf_bytes[0x9e:0xa0])[0]
print(f"slist_addr={slist_addr} packing={slist_packing} count={slist_count} empty_file={empty_file}")

def sec_data(sec): return data[interleave5(sec)*532+20 : interleave5(sec)*532+20+512]
def sec_tag(sec): return data[interleave5(sec)*532 : interleave5(sec)*532+20]

def csum_ok(sec):
    tag = sec_tag(sec); d = sec_data(sec)
    if not (struct.unpack('>H', tag[6:8])[0] & 0x8000): return "no-bit"
    c = 0
    for b in d: c ^= b
    for b in tag: c ^= b
    return c == 0

for s in range(1, empty_file):
    page = mddf + slist_addr + s//slist_packing
    off = (s % slist_packing)*14
    tb = sec_data(page)
    hintaddr = struct.unpack('>I', tb[off:off+4])[0]
    if hintaddr == 0: continue
    hs = mddf + hintaddr
    h = sec_data(hs)
    name = h[1:1+h[0]]
    try: name = name.decode('mac-roman')
    except: name = repr(name)
    mid = int.from_bytes(h[0x42:0x46],'big')
    prot = h[0x48]
    if prot != 0 or mid != 0:
        print(f"sfile {s:4d}: '{name}': hint={hs} machine_id={mid:#010x} protected={prot} csum_ok={csum_ok(hs)}")
