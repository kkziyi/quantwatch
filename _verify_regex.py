"""Verify StockScreener ST/new-stock regex"""
import pandas as pd

names = pd.Series(["贵州茅台", "宁德时代", "N华虹公司", "C中芯", "*ST康得", "ST保千里", "招商银行"])

mask1 = names.str.contains(r"^\*?ST|N\s|C\s", na=False)
mask2 = names.str.contains(r"^\*?ST|^N|^C", na=False)

print("Current regex (^\\*?ST|N\\s|C\\s):")
for n, m in zip(names, mask1):
    print(f"  {n}: {'FILTERED' if m else 'keep'} ")

print()
print("Fixed regex (^\\*?ST|^N|^C):")
for n, m in zip(names, mask2):
    print(f"  {n}: {'FILTERED' if m else 'keep'} ")

print()
print("BUG: N华虹公司 (new listing) should be filtered but current regex misses it")
print("BUG: C中芯 (days 2-5 listing) should be filtered but current regex misses it")
print("Fix: change N\\s|C\\s to ^N|^C in both top_gainers and top_losers")
