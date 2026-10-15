import importlib.util, io, os, struct
from datetime import datetime, timedelta
from contextlib import redirect_stdout
spec = importlib.util.spec_from_file_location("tool", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "LisaFileSystemTool.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
epoch = datetime(1901,1,1)

imgs = [
 "/tmp/lisaem-profile-5MB-FreshInstallOf_PascalWorkshop1.0.dc42",
 "/tmp/lisaem-profile-5MB-FreshInstallOf_LOS1.0.dc42",
]
rows = []
for img in imgs:
    with redirect_stdout(io.StringIO()):
        try: fs = mod.InMemoryFileSystem(img)
        except Exception: continue
    first,last = fs._flat_catalog_sfile_range()
    for s in range(first,last+1):
        e = fs._slist_entry(s)
        if e is None or e[0]==0: continue
        hs = fs._locate_hint_page_for_sfile(s, fs._mddf_sector_number+e[0])
        if hs is None: continue
        name = mod.pascal_to_string(fs.read_sector(hs), start=0)
        if not name.upper().endswith(".TEXT") or "/" in name: continue
        if fs.read_sector(hs)[0x2C] == 2: continue
        data = fs.flat_catalog_read_file_data(s)
        if len(data) < 1024: continue
        t = data[1024:]
        ln = -1
        for i in range(len(t)-1,-1,-1):
            if t[i]: ln=i; break
        rl = ln+1
        txt = t[:rl]
        lines = txt.split(b"\r")
        nonempty = sum(1 for l in lines if l)
        # hentry dates
        h = fs.read_sector(hs)
        dtc_c = struct.unpack(">I", h[0x2E:0x32])[0]
        dtc_m = struct.unpack(">I", h[0x36:0x3A])[0]
        hdr = data[512:512+68]
        f = [struct.unpack(">H", hdr[i:i+2])[0] for i in range(0,64,2)]
        dtc_hdr = struct.unpack(">I", hdr[0x40:0x44])[0]
        rows.append(dict(name=name, size=len(data), rl=rl, nlines=len(lines), ne=nonempty,
                         first=len(lines[0]), last=len(lines[-1]), f=f,
                         dtc_hdr=dtc_hdr, dtc_c=dtc_c, dtc_m=dtc_m,
                         created=epoch+timedelta(seconds=dtc_c), modified=epoch+timedelta(seconds=dtc_m),
                         hdtc=epoch+timedelta(seconds=dtc_hdr)))

# print field-by-field
names = [r["name"] for r in rows]
fields = ["+00","+02","+04","+06","+08","+0a","+0c","+0e","+10","+12","+14","+16","+18","+1a","+1c","+1e","+20","+22","+24","+26","+28","+2a","+2c","+2e","+30","+32","+34","+36","+38","+3a","+3c","+3e"]
w = 13
print(f"{'field':6} " + " ".join(f"{n[:12]:13}" for n in names))
for i,fl in enumerate(fields):
    print(f"{fl:6} " + " ".join(f"{r['f'][i]:013x}" for r in rows))
print()
stats = [("size", "size"),("reallen","rl"),("nlines","nlines"),("nonempty","ne"),("firstln","first"),("lastln","last")]
for lbl, k in stats:
    print(f"{lbl:6} " + " ".join(f"{str(r[k]):13}" for r in rows))
print()
print(f"{'hdrDTC':6} " + " ".join(f"{str(r['hdtc'])[:13]:13}" for r in rows))
print(f"{'created':6} " + " ".join(f"{str(r['created'])[:13]:13}" for r in rows))
print(f"{'modified':6} " + " ".join(f"{str(r['modified'])[:13]:13}" for r in rows))
