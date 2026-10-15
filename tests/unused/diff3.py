import sys
def load(p): return open(p,'rb').read()
a = load(sys.argv[1]); b = load(sys.argv[2])
print("sizes:", len(a), len(b))
n = min(len(a),len(b))//532
D=(0,4,8,12,0,4,8,-4,0,4,-8,-4,0,-12,-8,-4)
inv = {r+D[r&15]: r for r in range(n)}
diffs = {}
for R in range(n):
    sa = a[R*532:(R+1)*532]; sb = b[R*532:(R+1)*532]
    if sa != sb:
        pos = [i for i in range(532) if sa[i]!=sb[i]]
        diffs[R] = pos
print("num differing records:", len(diffs))
for R in sorted(diffs):
    L = inv.get(R, R)
    pos = diffs[R]
    tag_pos = [p for p in pos if p < 20]
    data_pos = [p-20 for p in pos if p >= 20]
    print(f"rec {R} (logical {L}): tag {tag_pos} data {data_pos}")
    if len(pos) <= 12:
        for p in pos:
            print(f"    {'tag ' if p<20 else 'data'}[{p if p<20 else p-20}] A={a[R*532+p]:#04x} B={b[R*532+p]:#04x}")
