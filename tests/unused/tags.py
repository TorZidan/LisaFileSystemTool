import importlib.util, io, os
from contextlib import redirect_stdout
spec = importlib.util.spec_from_file_location("tool", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "LisaFileSystemTool.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
img = "/tmp/lisaem-profile-5MB-FreshInstallOf_PascalWorkshop1.0.dc42"
with redirect_stdout(io.StringIO()):
    fs = mod.InMemoryFileSystem(img)
mddf = fs._mddf_sector_number
for sfile in (13, 14):
    e = fs._slist_entry(sfile)
    print(f"sfile {sfile}: hint={e[0]} fileaddr={e[1]} filesize={e[2]} version={e[3]}")
    rel = e[1]
    i = 0
    while rel:
        tag = fs.read_tags_for_sector(mddf + rel)
        du = tag[6:8]
        print(f"  data page rel={rel} abs={mddf+rel} tag_fileid={tag[4:6].hex()} dataused_raw={du.hex()} ({int.from_bytes(du,'big') & 0x7fff}) fwd={tag[14:17].hex()}")
        fwd = (tag[0x0E] << 16) | (tag[0x0F] << 8) | tag[0x10]
        rel = fwd if fwd != 0xFFFFFF else 0
        i += 1
