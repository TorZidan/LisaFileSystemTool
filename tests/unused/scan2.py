import struct, sys
path = sys.argv[1]
data = open(path, 'rb').read()
# raw or dc42?
magic = struct.unpack('>H', data[0x52:0x54])[0]
if magic == 0x0100:
    print(f"{path}: DC42"); sys.exit(0)
D=(0,4,8,12,0,4,8,-4,0,4,-8,-4,0,-12,-8,-4)
def il5(n): return n + D[n & 15]
n = len(data)//532
bad = []
for R in range(n):
    t = data[R*532:R*532+20]; d = data[R*532+20:R*532+532]
    x = 0
    for b in t: x ^= b
    for b in d: x ^= b
    if x != 0: bad.append(R)
print(f"{path}: {n} sectors, {len(bad)} bad: {bad}")
