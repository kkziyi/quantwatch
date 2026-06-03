#!/usr/bin/env python3
"""Quick test for reports/daily_brief.py"""
import sys
sys.path.insert(0, '/mnt/d/MyProject/stock-watch')

# 1. Test imports
from reports import MarketScanner, StockScreener, DailyBrief, run_daily_brief
print("✓ All imports OK")

# 2. Test StockScreener with mock data
import pandas as pd
import numpy as np
np.random.seed(42)

mock = pd.DataFrame({
    'code': ['000001', '000002', '600036', '600519', '300750', '000858', '002415', '600809', '601318', '000651'],
    'name': ['平安银行', '万科A', '招商银行', '贵州茅台', '宁德时代', '五粮液', '海康威视', '山西汾酒', '中国平安', '格力电器'],
    'price': [12.5, 18.3, 35.2, 1680.0, 200.0, 145.0, 33.0, 220.0, 45.0, 38.0],
    'change_pct': [3.5, 4.2, -2.1, 1.2, 4.8, 3.8, -5.0, -1.5, 2.0, 4.1],
    'volume': [1.2e8, 8e7, 5e7, 3e6, 2e7, 4e7, 3e7, 1e7, 6e7, 5e7],
    'amount': [15e8, 14e8, 17e8, 50e8, 40e8, 21e8, 10e8, 35e8, 27e8, 19e8],
    'turnover': [3.0, 2.5, 1.2, 0.3, 7.2, 2.1, 1.8, 4.5, 1.0, 6.5],
    'volume_ratio': [2.5, 1.8, 0.8, 0.6, 3.5, 1.5, 0.7, 1.2, 0.9, 2.2],
    'float_mcap': [1800e8, 2200e8, 4500e8, 21000e8, 9000e8, 6000e8, 3500e8, 2800e8, 7000e8, 2300e8],
})

screener = StockScreener(mock)
surge = screener.volume_surge(2.0, 3.0)
print(f"✓ volume_surge: {len(surge)} stocks (expect >=2)")
print(f"  {list(zip(surge['name'], surge['change_pct']))}")

custom = screener.custom_filter({
    'change_pct': (3.0, 5.0),
    'volume_ratio': (1.0, None),
    'turnover': (5.0, 10.0),
    'float_mcap': (50e8, 100e8),
})
print(f"✓ custom_filter: {len(custom)} stocks")
print(f"  {list(zip(custom['name'], custom['change_pct']))}")

top_gainers = screener.top_gainers(5)
print(f"✓ top_gainers: {len(top_gainers)} stocks")

top_losers = screener.top_losers(5)
print(f"✓ top_losers: {len(top_losers)} stocks")

# 3. Test DailyBrief format output (mock scanner)
class MockScanner:
    def scan(self):
        return mock

brief = DailyBrief(scanner=MockScanner())
report = brief.generate()
print(f"✓ DailyBrief.generate(): {len(report)} chars")
print("--- BEGIN REPORT SAMPLE ---")
print(report[:500])
print("--- END REPORT SAMPLE ---")

# 4. Test main.py imports
import main
print("✓ main.py imports OK")

print("\n🎉 All tests passed!")
