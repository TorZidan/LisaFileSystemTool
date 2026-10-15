import struct, sys
DELTA = (0, 4, 8, 12, 0, 4, 8, -4, 0, 4, -8, -4, 0, -12, -8, -4)
def il5(n): return n + DELTA[n & 15]
path = sys.argv[1]
data = open(path, 'rb').read()
nblocks = len(data)//532
boot = data[0*532+20 : 0*532+532]
print("boot[0:2] =", boot[0:2].hex(), " boot_id@4 =", hex(struct.unpack('>H', boot[4:6])[0]))
if struct.unpack('>H', boot[4:6])[0] == 0xAAAA:
    fsb0 = struct.unpack('>H', boot[14:16])[0]
else:
    fsb0 = struct.unpack('>H', boot[10:12])[0]
mddf = fsb0 + 8
print("fs_block0 =", fsb0, " MDDF sector =", mddf, " (phys", il5(mddf), ")")
m = data[il5(mddf)*532+20 : il5(mddf)*532+532]
fsv = m[0]
print("fsversion =", fsv)
def u16(o): return struct.unpack('>H', m[o:o+2])[0]
def u32(o): return struct.unpack('>I', m[o:o+4])[0]
print("first_file =", u16(0x90), " slist_addr(0x94) =", u32(0x94), " slist_packing(0x98) =", u16(0x98), " slist_blocks(0x9A) =", u16(0x9A))
print("empty_file(0x9E) =", u16(0x9E), " maxfiles(0xA0) =", u16(0xA0), " filecount(0xB0) =", u16(0xB0))
print("bitmap: first_rel(0x88) =", u32(0x88), " numbits(0x8C) =", u32(0x8C), " numbitmap(0x92) =", u16(0x92))
print("rootmaxentries(0xC0) =", u16(0xC0))
print("mddf volname:", m[0x104:0x124].split(b'\x00')[0])
