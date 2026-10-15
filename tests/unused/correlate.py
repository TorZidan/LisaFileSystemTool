import importlib.util, io, os, struct
from contextlib import redirect_stdout
spec = importlib.util.spec_from_file_location("tool", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "LisaFileSystemTool.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

imgs = [
 "/tmp/lisaem-profile-5MB-FreshInstallOf_PascalWorkshop1.0.dc42",
 "/tmp/lisaem-profile-5MB-FreshInstallOf_LOS1.0.dc42",
 "/tmp/lisaem-profile-5MB-FreshInstallOf_LOS2.0.dc42",
 "/tmp/lisaem-profile-5MB-FreshInstallOf_LOS3.1.dc42",
 "/tmp/lisaem-profile-5MB-Smalltalk.dc42",
 "/tmp/lisaem-profile-5MB-blank-original.dc42",
 "/tmp/lisaem-profile-5MB-ForDelete.dc42",
 "/tmp/50MB_LOS1.0andPascalWorkshop1.0.image",
]
rows = []
for img in imgs:
    if not os.path.exists(img): continue
    with redirect_stdout(io.StringIO()):
        try: fs = mod.InMemoryFileSystem(img)
        except Exception: continue
    base = os.path.basename(img)
    if fs.is_flat_catalog_volume():
        first,last = fs._flat_catalog_sfile_range()
        cand = []
        for s in range(first,last+1):
            e = fs._slist_entry(s)
            if e is None or e[0]==0: continue
            hs = fs._locate_hint_page_for_sfile(s, fs._mddf_sector_number+e[0])
            if hs is None: continue
            name = mod.pascal_to_string(fs.read_sector(hs), start=0)
            if name.upper().endswith(".TEXT") and "/" not in name and fs.read_sector(hs)[0x2C] != 2:
                cand.append((s,name,e[2]))
    else:
        # B-tree volume: use dump_catalog output? easier: walk slist-like via catalog? 
        # use _find_rootcatalog... complex; skip for now
        continue
    for s,name,size in cand:
        try:
            data = fs.flat_catalog_read_file_data(s)
        except Exception:
            continue
        if len(data) < 1024: continue
        hdr = data[512:576]  # 64-byte TEB
        text = data[1024:]
        # real text extent: last non-zero byte
        ln = -1
        for i in range(len(text)-1,-1,-1):
            if text[i]: ln=i; break
        rl = ln+1
        nr = text[:rl].count(b"\r")
        rows.append((base[:28], name, len(data), rl, nr, hdr.hex()))

print(f"{'img':30} {'name':28} {'size':>5} {'realln':>6} {'nlines':>6}  hdr64")
for r in rows:
    print(f"{r[0]:30} {r[1]:28} {r[2]:>5} {r[3]:>6} {r[4]:>6}  {r[5]}")
