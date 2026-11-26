#!/usr/bin/env python3
"""Compare all FILEID_LOADER (0xBBBB) sectors between two DC42 floppy images.
Sectors are aligned by their tag rel_num (position within the loader file)."""
import struct
import sys

HDR = 84
SEC = 512
LOADER = 0xBBBB


def load(path):
    img = open(path, "rb").read()
    data_size = struct.unpack(">I", img[0x40:0x44])[0]
    tag_size = struct.unpack(">I", img[0x44:0x48])[0]
    nsec = data_size // SEC
    tsize = tag_size // nsec
    data = img[HDR:HDR + data_size]
    tags = img[HDR + data_size:HDR + data_size + tag_size]
    return dict(path=path, nsec=nsec, tsize=tsize, data=data, tags=tags)


def tag(d, n):
    return d["tags"][n * d["tsize"]:(n + 1) * d["tsize"]]


def sector(d, n):
    return d["data"][n * SEC:(n + 1) * SEC]


def file_id(d, n):
    return struct.unpack(">H", tag(d, n)[4:6])[0]


def rel_num(d, n):
    return struct.unpack(">H", tag(d, n)[6:8])[0]


def hexdump(b, width=16):
    out = []
    for i in range(0, len(b), width):
        chunk = b[i:i + width]
        hexs = " ".join(f"{c:02x}" for c in chunk)
        asc = "".join(chr(c) if 32 <= c < 127 else "." for c in chunk)
        out.append(f"{i:04x}  {hexs:<{width*3}}  {asc}")
    return "\n".join(out)


def main():
    a = load(sys.argv[1])
    b = load(sys.argv[2])

    def loader_sectors(d):
        res = []
        for n in range(d["nsec"]):
            if file_id(d, n) == LOADER:
                res.append((rel_num(d, n), n))
        res.sort()
        return res

    la, lb = loader_sectors(a), loader_sectors(b)
    print(f"{a['path']}\n  0xBBBB sectors (rel_num, abs_sector): {la}")
    print(f"{b['path']}\n  0xBBBB sectors (rel_num, abs_sector): {lb}")

    # Build maps rel_num -> abs sector
    ma = dict(la)
    mb = dict(lb)
    common = sorted(set(ma) & set(mb))
    only_a = sorted(set(ma) - set(mb))
    only_b = sorted(set(mb) - set(ma))
    print(f"\ncommon rel_nums: {common}")
    print(f"only in A: {only_a}")
    print(f"only in B: {only_b}")

    total_diff = 0
    for rn in common:
        sa = sector(a, ma[rn])
        sb = sector(b, mb[rn])
        diffs = [i for i in range(SEC) if sa[i] != sb[i]]
        total_diff += len(diffs)
        status = "IDENTICAL" if not diffs else f"{len(diffs)} bytes differ"
        print(f"\nrel_num {rn}: A_sector {ma[rn]} vs B_sector {mb[rn]} -> {status}")
        if diffs:
            print("  first 48 differing offsets: " +
                  ", ".join(f"{i:02x}:{sa[i]:02x}->{sb[i]:02x}" for i in diffs[:48]))
            if len(diffs) <= 64:
                print("  full XOR of the sector:")
                print(hexdump(bytes(x ^ y for x, y in zip(sa, sb))))
    print(f"\nTOTAL differing bytes across common loader sectors: {total_diff}")


if __name__ == "__main__":
    main()
