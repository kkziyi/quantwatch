#!/usr/bin/env python3
"""生产验证：模拟交易日连续 6 轮轮询，验证稳定性 + 去重逻辑"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from strategies.price_alert import fetch_realtime_quotes, check_alerts
from config import STOCKS

ROUNDS = 6
start = time.time()
errors = 0

for i in range(1, ROUNDS + 1):
    t0 = time.time()
    try:
        df = fetch_realtime_quotes(list(STOCKS.keys()))
        alerts, wl = check_alerts(df)
        elapsed = (time.time() - t0) * 1000
        print(f"[ROUND {i}/{ROUNDS}] {len(df)} quotes, {len(alerts)} alerts, {elapsed:.0f}ms")
        for alert in alerts:
            code, name, price, pct, direction = alert[0], alert[1], alert[2], alert[3], alert[4]
            sign = "+" if direction == "up" else "-"
            print(f"  {name}({code}) {sign}{abs(pct)*100:.2f}% [{direction}]")
    except Exception as e:
        errors += 1
        print(f"[ROUND {i}/{ROUNDS}] ERROR: {e}")

    if i < ROUNDS:
        time.sleep(1.5)

# 检查去重状态
state_file = os.path.join(os.path.dirname(__file__), "data/feishu_sent.json")
if os.path.exists(state_file):
    with open(state_file) as f:
        state = json.load(f)
    print(f"\n去重状态: {len(state)} entries → {list(state.keys())}")

total = time.time() - start
print(f"\n{'='*50}")
print(f"结果: {ROUNDS} 轮, {errors} 错误, 总耗时 {total:.1f}s")
print(f"稳定性: {'PASS' if errors == 0 else 'FAIL'}")
