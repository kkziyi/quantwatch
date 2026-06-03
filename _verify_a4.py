"""Quick verification of the after-close scheduler changes."""
import py_compile
import sys

files = [
    "schedulers/__init__.py",
    "schedulers/after_close.py",
    "main.py",
]

for f in files:
    try:
        py_compile.compile(f, doraise=True)
        print(f"  OK: {f}")
    except py_compile.PyCompileError as e:
        print(f"  FAIL: {f}: {e}")
        sys.exit(1)

print("All files compile OK")

# Test import
from schedulers import AfterCloseScheduler
sch = AfterCloseScheduler()
print(f"AfterCloseScheduler initialized: {len(sch._tasks)} tasks registered (expected 0)")
print("Import OK")
