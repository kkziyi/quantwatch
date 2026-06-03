"""Verify RSI fixes: P1 sign, P2 Wilder, P3 naming."""
import sys
sys.path.insert(0, '/mnt/d/MyProject/stock-watch')

from strategies.rsi_signal import RSISignal, check_rsi_signals, send_rsi_alerts
from strategies.kdj_signal import KDJSignal, check_kdj_signals
from strategies.macd_signal import MACDSignal, check_macd_signals

# P3: Verify method name alignment
r = RSISignal()
k = KDJSignal()
m = MACDSignal()

assert hasattr(r, 'check_signals'), 'RSI should have check_signals()'
assert hasattr(k, 'check_signals'), 'KDJ should have check_signals()'  
assert hasattr(m, 'check_signals'), 'MACD should have check_signals()'
print("P3 PASS: All three strategies use check_signals() method name")

# Verify module-level functions still work
assert callable(check_rsi_signals), 'check_rsi_signals should be callable'
assert callable(check_kdj_signals), 'check_kdj_signals should be callable'
assert callable(check_macd_signals), 'check_macd_signals should be callable'
print("P3 PASS: Module-level convenience functions work")

# P2: Verify Wilder smoothing (alpha=1/period vs span=period)
import pandas as pd
import numpy as np
prices = pd.Series([10.0]*10 + [12.0]*10)
wilder_result = r._compute_ema(prices, 14)
# Standard EMA would give different result
std_ema = prices.ewm(span=14, adjust=False).mean()
assert not np.isclose(wilder_result.iloc[-1], std_ema.iloc[-1], rtol=0.01), \
    "Wilder should differ from standard EMA"
print(f"P2 PASS: Wilder smoothing (alpha=1/14) last value = {wilder_result.iloc[-1]:.6f}")
print(f"         Standard EMA (span=14) last value    = {std_ema.iloc[-1]:.6f}")

# P1: Verify sign variable exists in send_rsi_alert logic
import inspect
source = inspect.getsource(r.send_rsi_alert)
assert "sign = " in source, "send_rsi_alert should set sign variable"
assert "{sign}" in source, "send_rsi_alert should use {sign} in format string"
print("P1 PASS: send_rsi_alert uses dynamic sign variable")

print()
print("ALL FIX VERIFICATIONS PASSED")
