#!/usr/bin/env python3
"""Sanity check after KDJ fixes"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from strategies import KDJSignal, STRATEGY_REGISTRY
import inspect

print('KDJSignal import: OK')
print(f'STRATEGY_REGISTRY keys: {list(STRATEGY_REGISTRY.keys())}')
assert 'kdj_signal' in STRATEGY_REGISTRY, 'kdj_signal 不在注册表中'

k = KDJSignal()
print(f'KDJSignal: n={k.n}, enabled={k.enabled}, diff_gap_min={k.diff_gap_min}')
assert k.diff_gap_min == 1.5, f'期望 diff_gap_min=1.5, 实际={k.diff_gap_min}'
assert not hasattr(k, 'diff_gap_ratio'), '不应再有 diff_gap_ratio 属性'

sig = inspect.signature(k._check_gap_filter)
params = list(sig.parameters.keys())
print(f'_check_gap_filter 参数: {params}')
assert params == ['k_val', 'd_val'], f'期望 [k_val, d_val], 实际 {params}'

assert k.overbought == 80
assert k.oversold == 20

print()
print('All sanity checks passed!')
