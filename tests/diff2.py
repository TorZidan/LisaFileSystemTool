import struct
def load(p): return open(p,'rb').read()
a = load("/tmp/LOS1AndWorkshop-test-backup.image")
b = load("/tmp/50MB_blank.image")
print("sizes:", len(a), len(b))
n = min(len(a),len(b))//532
diffs = {}
for R in range(n):
    sa = a[R*532:(R+1)*532]; sb = b[R*532:(R+1)*532]
    if sa != sb:
        pos = [i for i in range(532) if sa[i]!=sb[i]]
        diffs[R] = pos
print("differing records:", sorted(diffs))
D=(0,4,8,12,0,4,8,-4,0,4,-8,-4,0,-12,-8,-4)
inv = {r+D[(r)&15]: r for r in range(n)}
for R in sorted(diffs):
    L = inv.get(R, R)
    pos = diffs[R]
    tag_pos = [p for p in pos if p < 20]
    data_pos = [p-20 for p in pos if p >= 20]
    print(f"rec {R} (logical {L}): tag bytes {tag_pos}, data bytes {data_pos}")
    for p in pos:
        off = p-20 if p>=20 else p
        print(f"    {'tag ' if p<20 else 'data'}[{p if p<20 else p-20}] backup={a[R*532+p]:#04x} blank={b[R*532+p]:#04x}")
