#!/usr/bin/env python3
"""Verify death cross zero-axis labels in macd_signal.py"""
import sys
sys.path.insert(0, '/mnt/d/MyProject/stock-watch')

# Read source to verify death cross zero-axis labels exist
with open('/mnt/d/MyProject/stock-watch/strategies/macd_signal.py', 'r') as f:
    source = f.read()

checks = [
    ("零上死叉（见顶）", "zero-above death cross with '见顶'"),
    ("零下死叉（弱势）", "zero-below death cross with '弱势'"),
    ("零上金叉（强势）", "zero-above golden cross preserved"),
    ("零下金叉（反弹）", "zero-below golden cross preserved"),
    ("dif_t > 0 and dea_t > 0", "death cross DIF>0/DEA>0 check (见顶)"),
    ("dif_t < 0 and dea_t < 0", "death cross DIF<0/DEA<0 check (弱势)"),
]

for text, desc in checks:
    assert text in source, f"MISSING: {text} ({desc})"
    print(f"  ✓ {desc}: '{text}'")

# Confirm confirm_days removed from config
with open('/mnt/d/MyProject/stock-watch/config.yaml', 'r') as f:
    assert 'confirm_days' not in f.read(), "confirm_days should not be in config.yaml"
    print("  ✓ config.yaml: confirm_days removed")

with open('/mnt/d/MyProject/stock-watch/config.example.yaml', 'r') as f:
    assert 'confirm_days' not in f.read(), "confirm_days should not be in config.example.yaml"
    print("  ✓ config.example.yaml: confirm_days removed")

print("\n=== DEATH CROSS ZERO-AXIS CHECKS PASSED ===")
