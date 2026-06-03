"""Quick import check — strategy + notifier only."""
import sys
sys.path.insert(0, '/mnt/d/MyProject/stock-watch')

# Core imports only
from strategies.price_alert import PriceAlert
from notifiers.feishu import send_alert, _should_send
import ast

print("Core imports: OK")

# Structural check
pa = PriceAlert()
for method in ['_is_suspended', '_is_limit_hit', '_is_trading_day', '_is_trading_time',
               'compute_change', 'check_alert']:
    assert hasattr(pa, method), f'Missing {method}'
print("PriceAlert structure: OK")

# Signature check
import inspect
params = list(inspect.signature(send_alert).parameters.keys())
assert 'is_limit' in params, f"send_alert missing is_limit: {params}"
print("send_alert signature: OK")

# Syntax check _test_verify
with open('/mnt/d/MyProject/stock-watch/_test_verify.py') as f:
    ast.parse(f.read())
print("_test_verify.py parse: OK")

print("\n=== PASSED ===")
