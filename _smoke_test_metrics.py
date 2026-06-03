#!/usr/bin/env python3
"""快速冒烟测试 — backtesting.metrics"""
from backtesting.metrics import calculate_metrics

result = {
    'trades': [
        {'date': '2025-01-02', 'code': 'A', 'action': 'buy', 'price': 10, 'shares': 1000, 'reason': 'x', 'fee': 2.5},
        {'date': '2025-01-05', 'code': 'A', 'action': 'sell', 'price': 12, 'shares': 1000, 'reason': 'x', 'fee': 15.0},
        {'date': '2025-01-03', 'code': 'B', 'action': 'buy', 'price': 10, 'shares': 1000, 'reason': 'x', 'fee': 2.5},
        {'date': '2025-01-06', 'code': 'B', 'action': 'sell', 'price': 11, 'shares': 1000, 'reason': 'x', 'fee': 13.75},
        {'date': '2025-01-04', 'code': 'C', 'action': 'buy', 'price': 10, 'shares': 1000, 'reason': 'x', 'fee': 2.5},
        {'date': '2025-01-07', 'code': 'C', 'action': 'sell', 'price': 9, 'shares': 1000, 'reason': 'x', 'fee': 11.25},
        {'date': '2025-01-08', 'code': 'D', 'action': 'buy', 'price': 10, 'shares': 1000, 'reason': 'x', 'fee': 2.5},
        {'date': '2025-01-09', 'code': 'D', 'action': 'sell', 'price': 13, 'shares': 1000, 'reason': 'x', 'fee': 16.25},
        {'date': '2025-01-10', 'code': 'E', 'action': 'buy', 'price': 10, 'shares': 1000, 'reason': 'x', 'fee': 2.5},
        {'date': '2025-01-11', 'code': 'E', 'action': 'sell', 'price': 14, 'shares': 1000, 'reason': 'x', 'fee': 17.5},
    ],
    'equity_curve': [
        {'date': '2025-01-01', 'total_value': 1000000, 'cash': 1000000, 'positions': {}},
        {'date': '2025-01-02', 'total_value': 1008000, 'cash': 990000, 'positions': {'A': 1000}},
        {'date': '2025-01-03', 'total_value': 1010000, 'cash': 980000, 'positions': {'A': 1000, 'B': 1000}},
        {'date': '2025-01-04', 'total_value': 1005000, 'cash': 970000, 'positions': {'A': 1000, 'B': 1000, 'C': 1000}},
        {'date': '2025-01-05', 'total_value': 1012000, 'cash': 982000, 'positions': {'B': 1000, 'C': 1000}},
        {'date': '2025-01-06', 'total_value': 1015000, 'cash': 993000, 'positions': {'C': 1000}},
        {'date': '2025-01-07', 'total_value': 1010000, 'cash': 1002000, 'positions': {}},
        {'date': '2025-01-08', 'total_value': 1012000, 'cash': 992000, 'positions': {'D': 1000}},
        {'date': '2025-01-09', 'total_value': 1018000, 'cash': 1005000, 'positions': {}},
        {'date': '2025-01-10', 'total_value': 1020000, 'cash': 995000, 'positions': {'E': 1000}},
        {'date': '2025-01-11', 'total_value': 1025000, 'cash': 1009000, 'positions': {}},
    ],
    'config': {'initial_cash': 1000000},
}
m = calculate_metrics(result)
print('=== 指标输出 ===')
for k, v in m.items():
    print(f'  {k}: {v}')
print()
print('import OK, 冒烟测试通过')
