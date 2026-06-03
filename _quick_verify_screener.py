"""快速验证导入和基本API"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from reports.screener import ScreenerEngine
from reports import ScreenerEngine as SE2

print("Import OK")
print("RULE_NAMES:", ScreenerEngine.RULE_NAMES)
e = ScreenerEngine()
print("__init__ re-export OK:", SE2 is ScreenerEngine)

# 模拟 screen 调用
from unittest.mock import MagicMock
scanner = MagicMock()
import pandas as pd
scanner.scan.return_value = pd.DataFrame()
e2 = ScreenerEngine(scanner=scanner)
results = e2.screen(rules=['volume_breakout', 'limit_up_analysis'])
print("screen() subset OK:", list(results.keys()))
print("All checks passed!")
