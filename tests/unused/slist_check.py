import struct
def il5(s):
    D=(0,4,8,12,0,4,8,-4,0,4,-8,-4,0,-12,-8,-4)
    return s + D[s & 15]
img = open("/tmp/50MB_blank.image","rb").read()
def sector(s): return img[il5(s)*532 : il5(s)*532+532]
def u16(b,o): return struct.unpack('>H', b[o:o+2])[0]
def u32(b,o): return struct.unpack('>I', b[o:o+4])[0]
# find MDDF: sector with file_id type 0x0001
mddf = None
for s in range(20, 100):
    t = sector(s)
    if struct.unpack('>H', t[0:2])[0] == 0x0010 or u16(t,2)==0x0001:
        pass
# scan first 50 sectors for tag file_id type == 1
for s in range(0, 60):
    t = sector(s)
    fid_type = u16(t,2)
    if fid_type == 0x0001:
        mddf = s
        break
print("MDDF at sector", mddf)
m = sector(mddf)
slist_addr = u32(m, 0x94); slist_packing = u16(m,0x98); slist_blocks = u16(m,0x9A)
empty_file = u16(m,158)
print(f"slist_addr={slist_addr} packing={slist_packing} blocks={slist_blocks} empty_file={empty_file}")
def slist_entry(sfile):
    page = mddf + slist_addr + sfile // slist_packing
    off = (sfile % slist_packing) * 14
    p = sector(page)
    return u32(p,off), u32(p,off+4), u32(p,off+8), u16(p,off+12)
# actual hint sectors (from prior analysis):
actual = {4:74, 66:2416, 75:288, 77:466, 85:660, 130:280}
names = {4:'rootcatalog', 66:'Editor.obj', 75:'Assembler.obj', 77:'Code.obj', 85:'Pascal.obj', 130:'BASIC.OBJ'}
for sf in sorted(actual):
    hintaddr, fileaddr, fsize, ver = slist_entry(sf)
    slist_sector = mddf + hintaddr if hintaddr else None
    tag_fid = struct.unpack('>h', sector(slist_sector)[4:6])[0] if slist_sector is not None else None
    ok = (slist_sector == actual[sf])
    print(f"sfile {sf:3d} ({names[sf]:14s}): slist hintaddr->sector {slist_sector} (tag file_id={tag_fid}, expect {-sf}), actual hint sector {actual[sf]}, {'OK' if ok else '** STALE **'}")
