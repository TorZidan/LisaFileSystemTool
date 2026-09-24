import struct
IMG = "/tmp/50MB_blank.image"
def interleave5(sector):
    d = (0, 4, 8, 12, 0, 4, 8, -4, 0, 4, -8, -4, 0, -12, -8, -4)
    return sector + d[sector & 15]
data = bytearray(open(IMG,'rb').read())

# For each failing sector: current byte11 (tool-written) vs needed (correct for zeroed data).
# If tool wrote OLD checksum, then: written = needed ^ XOR(changed bytes),
# where changed bytes = original machine_id(4) ^ original protected(1).
# We can't know original machine_id, but we CAN check consistency with the readback
# verification: the tool verified machine_id==0 on disk. So disk data is zeroed.
for sec in (280, 660, 2416):
    pos = interleave5(sec)*532
    tag = data[pos:pos+20]
    d = data[pos+20:pos+532]
    c = 0
    for b in d: c ^= b
    for i, b in enumerate(tag):
        if i != 11: c ^= b
    print(f"sector {sec}: tool wrote byte11=0x{tag[11]:02x}, correct value for current data=0x{c:02x}, delta=0x{tag[11]^c:02x}")
