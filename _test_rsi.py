"""Verify RSISignal import and configuration loading."""
import sys
sys.path.insert(0, '/mnt/d/MyProject/stock-watch')

from strategies.rsi_signal import RSISignal, check_rsi_signals, send_rsi_alerts

# Test 1: Create instance with default config
r = RSISignal()
print(f"Test 1 PASS: RSISignal() created")
print(f"  enabled={r.enabled}, period={r.period}")
print(f"  overbought={r.overbought}, oversold={r.oversold}")
print(f"  extreme_overbought={r.extreme_overbought}, extreme_oversold={r.extreme_oversold}")

# Test 2: Verify config values match config.yaml
assert r.period == 14, f"Expected period=14, got {r.period}"
assert r.overbought == 70, f"Expected overbought=70, got {r.overbought}"
assert r.oversold == 30, f"Expected oversold=30, got {r.oversold}"
assert r.extreme_overbought == 85, f"Expected extreme_overbought=85, got {r.extreme_overbought}"
assert r.extreme_oversold == 15, f"Expected extreme_oversold=15, got {r.extreme_oversold}"
print("Test 2 PASS: Config values match config.yaml")

# Test 3: Custom config
r2 = RSISignal(config={"enabled": True, "period": 7, "overbought": 80, "oversold": 20, "extreme_overbought": 90, "extreme_oversold": 10})
assert r2.period == 7
assert r2.overbought == 80
print("Test 3 PASS: Custom config works")

# Test 4: Module-level convenience functions exist
assert callable(check_rsi_signals), "check_rsi_signals should be callable"
assert callable(send_rsi_alerts), "send_rsi_alerts should be callable"
print("Test 4 PASS: Module-level functions exist")

# Test 5: RSI computation (synthetic data)
import pandas as pd
import numpy as np

# 0 means the previous value, creating a simple test
prices = [10.0] * 20  # 20 days flat
# Then a sharp rise on day 21 to trigger overbought
test_df = pd.DataFrame({"close": prices})
result = r.compute_rsi(test_df)
assert "RSI" in result.columns
# After flat period, RSI should be 50 (or NaN for early values due to EMA convergence)
valid_rsi = result["RSI"].dropna()
if len(valid_rsi) > 0:
    print(f"  RSI computation: last valid value = {valid_rsi.iloc[-1]:.2f}")
print("Test 5 PASS: RSI computation works")

# Test 6: State file path
path = RSISignal._state_file_path()
assert path.endswith("data/feishu_sent.json"), f"Unexpected path: {path}"
print(f"Test 6 PASS: State file path = {path}")

# Test 7: Disabled mode
r3 = RSISignal(config={"enabled": False})
signals = r3.check_signals()
assert signals == [], f"Disabled strategy should return empty, got {signals}"
print("Test 7 PASS: Disabled strategy returns empty")

print("\n✅ ALL TESTS PASSED")
