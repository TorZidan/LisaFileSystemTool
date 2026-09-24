import struct
def interleave5(sector):
    d = (0, 4, 8, 12, 0, 4, 8, -4, 0, 4, -8, -4, 0, -12, -8, -4)
    return sector + d[sector & 15]

def load(p):
    data = open(p,'rb').read()
    return data

blank = load("/tmp/50MB_blank.image")
work  = load("/tmp/50MB_LOS1.0andPascalWorkshop1.0.image")

for sec in (660, 2416, 280, 288, 466):
    pos = interleave5(sec)*532
    tb, db = blank[pos:pos+20], blank[pos+20:pos+532]
    tw, dw = work[pos:pos+20], work[pos+20:pos+532]
    diffs = [i for i in range(512) if db[i]!=dw[i]]
    print(f"sector {sec}: blank tag={tb.hex(' ')}")
    print(f"             work  tag={tw.hex(' ')}")
    print(f"             data diffs vs workshop: {len(diffs)} bytes at {diffs[:20]}")
    # old checksum of workshop (pre-modification state proxy)
    c = 0
    for b in dw: c ^= b
    for b in tw: c ^= b
    print(f"             workshop running csum (incl its byte11): {c:#04x} -> its byte11 valid: {c==0}")
