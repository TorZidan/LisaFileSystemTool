import importlib.util
import os
import sys
spec = importlib.util.spec_from_file_location("lft", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "LisaFileSystemTool.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
import io, contextlib
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    t = m.InMemoryFileSystem(sys.argv[1])
    prot = t.find_protected_files()
print("protected hint sectors:", prot)
