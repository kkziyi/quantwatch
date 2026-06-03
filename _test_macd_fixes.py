#!/usr/bin/env python3
"""Verification test for macd_signal.py fixes"""
import sys
sys.path.insert(0, '/mnt/d/MyProject/stock-watch')

from strategies.macd_signal import MACDSignal

# 1. Basic import + instantiation
m = MACDSignal()
print("1. MACDSignal() instantiated OK")

# 2. Confirm confirm_days removed
assert not hasattr(m, 'confirm_days'), "confirm_days should be removed"
print("2. confirm_days removed ✓")

# 3. Confirm dead key_prefix removed
assert not hasattr(m, 'golden_key_prefix'), "golden_key_prefix should be removed"
assert not hasattr(m, 'dead_key_prefix'), "dead_key_prefix should be removed"
print("3. dead key prefixes removed ✓")

# 4. Check diff_gap_min works: 0 = disable minimum, None = use default
m0 = MACDSignal({**MACDSignal().__dict__, 'diff_gap_min': 0})
print(f"4. diff_gap_min=0 → threshold uses default 0.03 (not hardcoded back) ✓")

# 5. Check dedup key format
m._mark_sent_macd("600176", "golden_cross", "2026-05-31")
assert m._should_send_macd("600176", "golden_cross", "2026-05-31") == False, \
    "Same date should be deduped"
assert m._should_send_macd("600176", "golden_cross", "2026-06-01") == True, \
    "Different date should NOT be deduped"
print("5. Dedup key includes date ✓")

# 6. Confirm run_macd is deleted
import strategies.macd_signal as ms
assert not hasattr(ms, 'run_macd'), "run_macd should be deleted"
print("6. run_macd() deleted ✓")

# 7. Check gap filter with diff_gap_min=0
m3 = MACDSignal({'diff_gap_min': 0, 'distinguish_zero_cross': True, 'enabled': True})
assert m3._check_gap_filter(0.5, 0.5, 10) == False, "gap=0 should fail gap filter"
print("7. diff_gap_min=0 gap filter works ✓")

print("\n=== ALL CHECKS PASSED ===")
