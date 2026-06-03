#!/usr/bin/env python3
"""集成测试：行情获取 → 阈值检测 → 飞书推送（模拟）"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from strategies.price_alert import fetch_realtime_quotes, check_alerts, get_quote_summary
from config import STOCKS, FEISHU_WEBHOOK_URL

print("=" * 50)
print("QuantWatch 集成测试")
print("=" * 50)

# 1. 行情获取
print("\n[1/3] 获取实时行情...")
df = fetch_realtime_quotes(list(STOCKS.keys()))
print(f"  成功: {len(df)} 只股票")
for _, row in df.iterrows():
    print(f"  {row['代码']} {row['名称']:<10s} "
          f"现价={row['最新价']:.2f} "
          f"涨跌={row['涨跌幅']:+.2f}% "
          f"昨收={row['昨收']:.2f}")

# 2. 阈值检测
print("\n[2/3] 阈值检测...")
alerts, watchlist = check_alerts(df)
if alerts:
    print(f"  ⚠️ {len(alerts)} 条异动触发:")
    for alert in alerts:
        code, name, price, pct, direction = alert[0], alert[1], alert[2], alert[3], alert[4]
        is_limit = alert[5] if len(alert) >= 6 else False
        sign = "+" if direction == "up" else "-"
        print(f"    {name}({code}) {sign}{abs(pct)*100:.2f}% 现价={price:.2f} [{direction}]")
else:
    print("  ✓ 无触发，全部在阈值内")

# 3. 飞书推送（如果配置了 webhook）
print("\n[3/3] 飞书推送...")
if FEISHU_WEBHOOK_URL:
    from notifiers.feishu import send_summary
    if alerts:
        ok = send_summary(alerts)
        print(f"  {'✓ 推送成功' if ok else '✗ 推送失败'}")
    else:
        print("  - 无警报，跳过推送")
else:
    print("  ⚠ 飞书 Webhook 未配置（编辑 config.py 中的 FEISHU_WEBHOOK_URL）")
    if alerts:
        print(f"  （模拟推送：{len(alerts)} 条异动将被发送）")

# 4. 日志摘要
print("\n" + "=" * 50)
print("行情摘要:")
print(get_quote_summary(watchlist))
print("=" * 50)

if not df.empty:
    print("\n✅ 全链路测试通过！")
else:
    print("\n❌ 行情获取失败")
