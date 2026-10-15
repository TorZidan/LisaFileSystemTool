import importlib.util, io, os
from contextlib import redirect_stdout
spec = importlib.util.spec_from_file_location("tool", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "LisaFileSystemTool.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

def show(name, img, sfile):
    with redirect_stdout(io.StringIO()):
        fs = mod.InMemoryFileSystem(img)
    data = fs.flat_catalog_read_file_data(sfile)
    hdr = data[:1024]
    out = f"/tmp/hdr_{name}.bin"
    open(out,"wb").write(hdr)
    print(f"== {name} (sfile={sfile}, total={len(data)})")
    for off in (0, 512):
        print(f"  sector at {off:#x}:")
        for row in range(8):
            b = hdr[off+row*16: off+row*16+16]
            hexs = b.hex(" ")
            asc = "".join(chr(c) if 32<=c<127 else "." for c in b)
            print(f"    {off+row*16:04x}: {hexs}  {asc}")
    open(f"/tmp/text_{name}.bin","wb").write(data[1024:])

img1 = "/tmp/lisaem-profile-5MB-FreshInstallOf_PascalWorkshop1.0.dc42"
img2 = "/tmp/lisaem-profile-5MB-FreshInstallOf_LOS1.0.dc42"
show("EDIT_MENUS", img1, 8)
show("QD_BOXES", img1, 53)
show("QD_M_BOXES", img1, 62)
show("PAPER", img1, 80)
show("TERM_MENUS", img1, 85)
show("FLR_MENUS", img2, 26)
