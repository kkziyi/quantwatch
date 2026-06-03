import py_compile, sys
files = [
    'strategies/price_alert.py',
    'notifiers/feishu.py', 
    'main.py',
    '_test_verify.py',
    'test_integration.py',
    '_verify_prod.py',
]
ok = 0
for f in files:
    try:
        py_compile.compile(f, doraise=True)
        print(f"  {f}: OK")
        ok += 1
    except py_compile.PyCompileError as e:
        print(f"  {f}: FAIL - {e}")
        sys.exit(1)
print(f"\nAll {ok}/{len(files)} files compile OK")
