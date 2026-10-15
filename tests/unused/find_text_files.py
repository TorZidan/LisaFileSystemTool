import importlib.util, sys, glob, os
spec = importlib.util.spec_from_file_location("tool", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "LisaFileSystemTool.py"))
mod = importlib.util.module_from_spec(spec)
import io
from contextlib import redirect_stdout
spec.loader.exec_module(mod)

buf = io.StringIO()
imgs = [
 "/tmp/lisaem-profile-5MB-FreshInstallOf_PascalWorkshop1.0.dc42",
 "/tmp/lisaem-profile-5MB-FreshInstallOf_LOS1.0.dc42",
 "/tmp/lisaem-profile-5MB-FreshInstallOf_LOS3.1.dc42",
 "/tmp/lisaem-profile-5MB-Smalltalk.dc42",
 "/tmp/lisaem-profile.dc42",
]
for img in imgs:
    if not os.path.exists(img):
        print(f"MISSING {img}"); continue
    with redirect_stdout(io.StringIO()):
        try:
            fs = mod.InMemoryFileSystem(img)
        except Exception as e:
            print(f"ERR {img}: {e}"); continue
    print(f"== {os.path.basename(img)} (fs_version={fs._fs_version}, flat={fs.is_flat_catalog_volume()})")
    if fs.is_flat_catalog_volume():
        first,last = fs._flat_catalog_sfile_range()
        for s in range(first,last+1):
            e = fs._slist_entry(s)
            if e is None: continue
            hintaddr, fileaddr, filesize, version = e
            if hintaddr == 0: continue
            hs = fs._locate_hint_page_for_sfile(s, fs._mddf_sector_number+hintaddr)
            if hs is None: continue
            name = mod.pascal_to_string(fs.read_sector(hs), start=0)
            if name.upper().endswith(".TEXT"):
                print(f"  sfile={s} name={name!r} size={filesize} hint={hs} fileaddr={fileaddr}")
    else:
        with redirect_stdout(io.StringIO()) as b:
            fs.dump_catalog()
        for line in b.getvalue().splitlines():
            if ".TEXT" in line.upper():
                print("  ", line[:200])
