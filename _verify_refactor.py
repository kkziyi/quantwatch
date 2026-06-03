"""Verify all imports and core functions work correctly after Phase 1a refactor."""
import sys
sys.path.insert(0, '/mnt/d/MyProject/stock-watch')

from strategies.price_alert import PriceAlert, check_alerts, get_quote_summary
from notifiers.feishu import send_summary, send_alert, _should_send, _mark_sent
import main

print("All imports: OK")
print(f"PriceAlert class: {PriceAlert}")

# Verify trading time delegate works
print(f"main.is_trading_time() = {main.is_trading_time()}")

# Verify class instantiation
pa = PriceAlert()
print(f"PriceAlert instance: {pa}")

# Verify module-level convenience functions
print(f"check_alerts: {check_alerts}")
print(f"get_quote_summary: {get_quote_summary}")

# Verify new methods exist
for method in ['_is_suspended', '_is_limit_hit', '_is_trading_day', '_is_trading_time',
               'compute_change', 'check_alert']:
    assert hasattr(pa, method), f"Missing: {method}"
    print(f"  pa.{method}: OK")

# Verify send_alert accepts is_limit param
import inspect
sig = inspect.signature(send_alert)
params = list(sig.parameters.keys())
print(f"send_alert params: {params}")
assert 'is_limit' in params, "send_alert missing is_limit parameter"

print("\n=== VERIFICATION PASSED ===")
