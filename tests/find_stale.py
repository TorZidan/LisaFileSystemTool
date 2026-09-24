import struct
D=(0,4,8,12,0,4,8,-4,0,4,-8,-4,0,-12,-8,-4)
def il5(n): return n + D[n & 15]
data = open("/tmp/50MB_blank.image",'rb').read()
n = len(data)//532
# data XOR of every sector, and tag-no-11 XOR of every sector
dxor = [0]*n
txor = [0]*n
b11  = [0]*n
fid  = [0]*n
for R in range(n):
    t = data[R*532:R*532+20]; d = data[R*532+20:R*532+532]
    txor[R] = 0
    for b in t[:11]+t[12:]: txor[R] ^= b
    dxor[R] = 0
    for b in d: dxor[R] ^= b
    b11[R] = t[11]
    fid[R] = struct.unpack('>H', t[4:6])[0]

failing = [280, 660, 2416]
for S in failing:
    # physical record of sector S
    R = il5(S)
    written = b11[R]
    # data-XOR that the written byte implies (with the FINAL tag):
    implied = written ^ txor[R]
    # also: what final data XOR gives correct checksum:
    correct = txor[R] ^ dxor[R]  # should equal 0 if sector valid... 
    # find sectors T whose data XOR == implied
    hits = [T for T in range(n) if dxor[T] == implied]
    print(f"S={S} (rec {R}): written b11={written:#04x}, implied data-XOR={implied:#04x}")
    print(f"   current data-XOR of S = {dxor[R]:#04x};  correct b11 should be {txor[R]^dxor[R]:#04x}")
    print(f"   sectors with data-XOR == implied: {hits[:20]}")
    # map hits to logical sectors
    inv = {il5(L): L for L in range(n)}
    print(f"   logical sectors: {[inv.get(h, f'rec{h}') for h in hits[:20]]}")
