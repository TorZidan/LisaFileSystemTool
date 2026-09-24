import struct
def il5(s):
    D=(0,4,8,12,0,4,8,-4,0,4,-8,-4,0,-12,-8,-4)
    return s + D[s & 15]
img = open("/tmp/50MB_blank.image","rb").read()
def sector(s): return img[il5(s)*532 : il5(s)*532+532]
def u16(b,o): return struct.unpack('>H', b[o:o+2])[0]
def u16s(b,o): return struct.unpack('>h', b[o:o+2])[0]
def u32(b,o): return struct.unpack('>I', b[o:o+4])[0]
# scan for MDDF: tag file_id == 1 (unsigned) — check first 200 sectors
cands = []
for s in range(0, 200):
    t = sector(s)
    fid = u16s(t,4)
    if fid in (1, 0xFFFF, 65535):
        cands.append((s, fid))
print("MDDF candidates (file_id==1):", cands[:10])
# per summary MDDF is at sector 38; check tag there
print("sector 38 tag:", sector(38)[:20].hex())
