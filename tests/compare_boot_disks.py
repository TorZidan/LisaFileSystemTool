#!/usr/bin/env python3
"""Compare DC42 floppy disk images: header, boot sector, MDDF, tags."""
import struct
import sys

HDR = 84
SEC = 512


def cksum(b):
    s = 0
    for i in range(0, len(b) - (len(b) % 2), 2):
        s = (s + (b[i] << 8 | b[i + 1])) & 0xFFFFFFFF
        s = ((s >> 1) | ((s & 1) << 31)) & 0xFFFFFFFF
    return s


def load(path):
    img = open(path, "rb").read()
    name = img[1:1 + img[0]].rstrip(b"\x00").decode("mac_roman", "replace")
    data_size = struct.unpack(">I", img[0x40:0x44])[0]
    tag_size = struct.unpack(">I", img[0x44:0x48])[0]
    data_ck = struct.unpack(">I", img[0x48:0x4C])[0]
    tag_ck = struct.unpack(">I", img[0x4C:0x50])[0]
    dtype = img[0x50]
    fmt = img[0x51]
    magic = struct.unpack(">H", img[0x52:0x54])[0]
    nsec = data_size // SEC
    tsize = tag_size // nsec
    data = img[HDR:HDR + data_size]
    tags = img[HDR + data_size:HDR + data_size + tag_size]
    return dict(path=path, name=name, data_size=data_size, tag_size=tag_size,
                data_ck=data_ck, tag_ck=tag_ck, dtype=dtype, fmt=fmt, magic=magic,
                nsec=nsec, tsize=tsize, data=data, tags=tags,
                data_ck_ok=(cksum(data) == data_ck),
                tag_ck_ok=(cksum(tags[tsize:]) == tag_ck))


def mddf_sector(d):
    b0 = d["data"][:SEC]
    boot_id = struct.unpack(">H", b0[4:6])[0]
    if boot_id == 0xAAAA:
        return struct.unpack(">H", b0[14:16])[0]
    if b0[0:2] == b"\x4e\xfa":
        return struct.unpack(">H", b0[10:12])[0]
    return -1


def sector(d, n):
    return d["data"][n * SEC:(n + 1) * SEC]


def tag(d, n):
    return d["tags"][n * d["tsize"]:(n + 1) * d["tsize"]]


def hexdump(b, width=16):
    out = []
    for i in range(0, len(b), width):
        chunk = b[i:i + width]
        hexs = " ".join(f"{c:02x}" for c in chunk)
        asc = "".join(chr(c) if 32 <= c < 127 else "." for c in chunk)
        out.append(f"{i:04x}  {hexs:<{width*3}}  {asc}")
    return "\n".join(out)


def main():
    paths = sys.argv[1:]
    disks = [load(p) for p in paths]
    for d in disks:
        print("=" * 100)
        print(f"{d['path']}")
        print(f"  name={d['name']!r} sectors={d['nsec']} tagsize={d['tsize']} "
              f"dtype=0x{d['dtype']:02x} fmt=0x{d['fmt']:02x} magic=0x{d['magic']:04x}")
        print(f"  data_ck=0x{d['data_ck']:08x} {'OK' if d['data_ck_ok'] else 'BAD!'} "
              f"tag_ck=0x{d['tag_ck']:08x} {'OK' if d['tag_ck_ok'] else 'BAD!'}")
        m = mddf_sector(d)
        print(f"  MDDF sector: {m}")
        if 0 <= m < d["nsec"]:
            md = sector(d, m)
            print(f"  MDDF tag: {tag(d, m).hex()}")
            print(f"  MDDF fsversion={struct.unpack('>H', md[0:2])[0]}")
            volname = md[12:12 + md[12]].rstrip(b"\x00").decode("mac_roman", "replace")
            print(f"  MDDF volname={volname!r}")
            for off, nm in [(108, "firstblock"), (110, "lastblock"), (116, "blockcount"),
                            (128, "MDDFaddr"), (132, "MDDFsize"), (136, "bitmap_addr"),
                            (148, "slist_addr"), (152, "slist_packing"), (154, "slist_block_count"),
                            (156, "first_file"), (158, "empty_file"), (160, "maxfiles"),
                            (176, "filecount"), (186, "freecount"), (190, "rootsnum"),
                            (192, "rootmaxentries"), (302, "root_page")]:
                v = struct.unpack(">H" if nm not in ("firstblock", "lastblock", "blockcount",
                                                     "MDDFaddr", "MDDFsize", "bitmap_addr",
                                                     "slist_addr", "freecount") else ">I",
                                  md[off:off + (4 if nm in ("firstblock", "lastblock", "blockcount",
                                                            "MDDFaddr", "MDDFsize", "bitmap_addr",
                                                            "slist_addr", "freecount") else 2)])[0]
                print(f"    {nm} = {v}")
    # Compare sector 0 (boot block) between disks
    if len(disks) >= 2:
        print("=" * 100)
        print("BOOT SECTOR 0 comparison:")
        for d in disks:
            b = sector(d, 0)
            print(f"\n--- {d['path']} ---")
            print(hexdump(b))
        # pairwise diff
        a, b = disks[0], disks[1]
        sa, sb = sector(a, 0), sector(b, 0)
        diffs = [i for i in range(SEC) if sa[i] != sb[i]]
        print(f"\nDiff of sector 0 ({a['path'].split('/')[-1]} vs {b['path'].split('/')[-1]}): "
              f"{len(diffs)} bytes differ")
        if diffs:
            print("  first 64 differing bytes: " +
                  ", ".join(f"{i:02x}:{sa[i]:02x}->{sb[i]:02x}" for i in diffs[:64]))
    # MDDF comparison
    if len(disks) >= 2:
        ma, mb = mddf_sector(disks[0]), mddf_sector(disks[1])
        if 0 <= ma < disks[0]["nsec"] and 0 <= mb < disks[1]["nsec"]:
            sa = sector(disks[0], ma)
            sb = sector(disks[1], mb)
            diffs = [i for i in range(SEC) if sa[i] != sb[i]]
            print(f"\nMDDF sector diff ({ma} vs {mb}): {len(diffs)} bytes differ")
            if diffs:
                print(hexdump(bytes(x ^ y for x, y in zip(sa, sb))))
    # Tag comparison
    if len(disks) >= 2:
        a, b = disks[0], disks[1]
        n = min(a["nsec"], b["nsec"])
        diffs = 0
        first_diffs = []
        for i in range(n):
            ta, tb = tag(a, i), tag(b, i)
            if ta != tb:
                diffs += 1
                if len(first_diffs) < 20:
                    first_diffs.append((i, ta.hex(), tb.hex()))
        print(f"\nTag diffs: {diffs} of {n} sectors have different tags")
        for i, ta, tb in first_diffs:
            print(f"  sector {i}: {ta} vs {tb}")


if __name__ == "__main__":
    main()
