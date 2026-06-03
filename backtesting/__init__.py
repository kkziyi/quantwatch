"""QuantWatch 历史回测模块 — 模拟策略在历史数据上的表现"""
from backtesting.engine import BacktestEngine
from backtesting.metrics import calculate_metrics

__all__ = ["BacktestEngine", "calculate_metrics"]
