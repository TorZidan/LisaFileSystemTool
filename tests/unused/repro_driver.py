import importlib.util
import os
import sys, io, contextlib
spec = importlib.util.spec_from_file_location("lst", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "LisaFileSystemTool.py"))
lst = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lst)

fs = lst.InMemoryFileSystem("/tmp/repro2.image")

# Force the same hint sectors the blank image had (rootcatalog 74 + the 4 protected .obj files)
forced = [74, 2416, 288, 466, 660]
fs.find_protected_files = lambda: forced

# Feed "y" to the prompt, capture output
stdin_backup = sys.stdin
sys.stdin = io.StringIO("y\n")
try:
    fs.remove_file_protection()
finally:
    sys.stdin = stdin_backup
print("=== driver done ===")
