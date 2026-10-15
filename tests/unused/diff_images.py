import struct
def interleave5(sector):
    d = (0, 4, 8, 12, 0, 4, 8, -4, 0, 4, -8, -4, 0, -12, -8, -4)
    return sector + d[sector & 15]
blank = open("/tmp/50MB_blank.image",'rb').read()
fresh = open("/tmp/50MB_FreshInstallOfLOS1.0.image",'rb').read()
assert len(blank)==len(fresh), (len(blank), len(fresh))
num = len(blank)//532
diffs = []
for sec in range(num):
    pos = interleave5(sec)*532
    if blank[pos:pos+532] != fresh[pos:pos+532]:
        bd = blank[pos+20:pos+532]; fd = fresh[pos+20:pos+532]
        byte_diffs = [i for i in range(512) if bd[i]!=fd[i]]
        tb = blank[pos:pos+20]; tf = fresh[pos:pos+20]
        tag_diffs = [i for i in range(20) if tb[i]!=tf[i]]
        diffs.append((sec, tag_diffs, byte_diffs))
print(f"{len(diffs)} sectors differ")
for sec, td, dd in diffs:
    print(f"sector {sec}: tag bytes {td}, data bytes {dd}")
