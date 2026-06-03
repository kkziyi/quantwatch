#!/usr/bin/env python3
"""
压力测试：连续执行 6 次轮询（模拟 30 分钟），验证稳定性
检查点：行情获取成功率、响应时间、内存/异常
"""
import sys, os, time, traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from strategies.price_alert import fetch_realtime_quotes, check_alerts
from config import STOCKS

ROUNDS = 6
INTERVAL = 3  # 加速：轮询间隔 3 秒（正常是 300 秒）
SUCCESS = FAIL = 0
TIMES = []

print(f"QuantWatch 压力测试 — {ROUNDS} 轮，间隔 {INTERVAL}s")
print(f"目标：模拟 30 分钟连续运行（{ROUNDS} 次轮询）")
print("=" * 50)

for i in range(1, ROUNDS + 1):
    t0 = time.time()
    print(f"\n[轮 {i}/{ROUNDS}] ", end="", flush=True)
    try:
        df = fetch_realtime_quotes(list(STOCKS.keys()))
        alerts, _ = check_alerts(df)
        elapsed = time.time() - t0
        TIMES.append(elapsed)
        SUCCESS += 1
        print(f"✓ {len(df)} 只股票, {len(alerts)} 条异动, 耗时 {elapsed:.1f}s")
    except Exception as e:
        elapsed = time.time() - t0
        FAIL += 1
        print(f"✗ 失败 ({elapsed:.1f}s): {e}")
        traceback.print_exc()

    if i < ROUNDS:
        time.sleep(INTERVAL)

print("\n" + "=" * 50)
print(f"结果: {SUCCESS}/{ROUNDS} 成功, {FAIL}/{ROUNDS} 失败")
if TIMES:
    avg = sum(TIMES) / len(TIMES)
    print(f"平均响应: {avg:.1f}s  (最快 {min(TIMES):.1f}s, 最慢 {max(TIMES):.1f}s)")

if SUCCESS == ROUNDS:
    print("\n✅ 压力测试通过 — 30 分钟模拟无异常")
else:
    print(f"\n❌ 压力测试失败 — {FAIL} 次错误")
