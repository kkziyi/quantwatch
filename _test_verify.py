"""Quick test script for price_alert.py — logic validation with mocked data."""
import sys
sys.path.insert(0, "/mnt/d/MyProject/stock-watch")

try:
    import pandas as pd
    print("pandas: OK")
except ImportError as e:
    print(f"pandas: MISSING ({e})")

try:
    import akshare
    print("akshare: OK")
except ImportError as e:
    print(f"akshare: MISSING ({e})")

try:
    from strategies.price_alert import PriceAlert
    print("PriceAlert import: OK")
except Exception as e:
    print(f"PriceAlert import: FAIL ({e})")
    sys.exit(1)

# ------ Logic Tests ------
import datetime as dt_module

pa = PriceAlert()

# Test 1: Trading time
mon_10am = dt_module.datetime(2026, 5, 25, 10, 0, 0)  # Mon
assert pa.is_trading_day(mon_10am), "FAIL: Mon not trading day"
assert pa.is_trading_time(mon_10am), "FAIL: 10:00 not trading"
print("  t1: Trading time Mon 10:00 OK")

sat = dt_module.datetime(2026, 5, 30, 10, 0, 0)
assert not pa.is_trading_day(sat), "FAIL: Sat is trading day"
print("  t2: Weekend skip OK")

early = dt_module.datetime(2026, 5, 25, 9, 0, 0)
assert not pa.is_trading_time(early), "FAIL: 9:00 is trading"
print("  t3: Outside hours OK")

lunch = dt_module.datetime(2026, 5, 25, 12, 0, 0)
assert not pa.is_trading_time(lunch), "FAIL: lunch is trading"
print("  t4: Lunch break OK")

# Test 2: Suspended
suspended_row = pd.Series({"成交量": 0, "最新价": 10.0})
assert pa._is_suspended(suspended_row), "FAIL: zero vol not suspended"
suspended_row2 = pd.Series({"成交量": float("nan"), "最新价": 10.0})
assert pa._is_suspended(suspended_row2), "FAIL: NaN vol not suspended"
normal_row = pd.Series({"成交量": 100000, "最新价": 10.0})
assert not pa._is_suspended(normal_row), "FAIL: normal row is suspended"
print("  t5: Suspended detection OK")

# Test 3: Limit hit
hit_row = pd.Series({"代码": "600176", "最新价": 11.0})
is_lim, direction = pa._is_limit_hit(hit_row, 10.0)
assert is_lim and direction == "up", f"FAIL: limit up not detected ({is_lim}, {direction})"
print("  t6: Limit up OK")

hit_row2 = pd.Series({"代码": "600176", "最新价": 9.0})
is_lim2, dir2 = pa._is_limit_hit(hit_row2, 10.0)
assert is_lim2 and dir2 == "down", f"FAIL: limit down not detected"
print("  t7: Limit down OK")

normal_row2 = pd.Series({"代码": "600176", "最新价": 10.3})
is_lim3, _ = pa._is_limit_hit(normal_row2, 10.0)
assert not is_lim3, "FAIL: +3% flagged as limit"
print("  t8: Not limit OK")

# Test 4: ChiNext/STAR limits
gem_row = pd.Series({"代码": "300750", "最新价": 12.0})
is_lim4, dir4 = pa._is_limit_hit(gem_row, 10.0)
assert is_lim4 and dir4 == "up", f"FAIL: ChiNext 20% limit ({is_lim4}, {dir4})"
print("  t9: ChiNext limit OK")

star_row = pd.Series({"代码": "688001", "最新价": 8.0})
is_lim5, dir5 = pa._is_limit_hit(star_row, 10.0)
assert is_lim5 and dir5 == "down", f"FAIL: STAR limit ({is_lim5}, {dir5})"
print("  t10: STAR limit OK")

# Test 5: Compute change
q_row = pd.Series({"代码": "600176", "名称": "中国巨石", "最新价": 10.35, "昨收": 10.0})
info = pa.compute_change(q_row)
assert abs(info["change_pct"] - 0.035) < 1e-9, f"FAIL: change_pct={info['change_pct']}"
assert abs(info["change_amt"] - 0.35) < 1e-9, f"FAIL: change_amt={info['change_amt']}"
print("  t11: Compute change OK")

# Test 6: Alert pure threshold detection (去重已移至飞书层 _should_send())
# First trigger: +3.5%
info["change_pct"] = 0.035
a1 = pa.check_alert(info)
assert a1 is not None, "FAIL: first alert not triggered"
assert a1["direction"] == "up", f"FAIL: wrong direction {a1.get('direction')}"
print("  t12: Up alert triggered OK")

# Bigger move: still triggers (纯阈值，不去重)
info["change_pct"] = 0.04
a2 = pa.check_alert(info)
assert a2 is not None, "FAIL: +4% alert not triggered"
assert a2["direction"] == "up"
print("  t13: Bigger move also triggers OK (dedup at feishu layer)")

# Recovery: back within threshold → 不触发
info["change_pct"] = 0.02
a3 = pa.check_alert(info)
assert a3 is None, "FAIL: recovery triggered alert"
print("  t14: Recovery no-alert OK")

# Reverse direction: down 3.5%
info["change_pct"] = -0.035
a4 = pa.check_alert(info)
assert a4 is not None, "FAIL: down alert not triggered"
assert a4["direction"] == "down"
print("  t15: Reverse direction alert OK")

# Deeper down: still triggers (纯阈值)
info["change_pct"] = -0.05
a5 = pa.check_alert(info)
assert a5 is not None, "FAIL: -5% alert not triggered"
assert a5["direction"] == "down"
print("  t16: Deeper down also triggers OK (dedup at feishu layer)")

print("\n=== ALL 16 TESTS PASSED ===")
