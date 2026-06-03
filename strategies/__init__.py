"""QuantWatch 策略模块

策略注册表：所有监控策略在此统一管理。
"""
from strategies.price_alert import PriceAlert
from strategies.volume_alert import VolumeAlert
from strategies.turnover_alert import TurnoverAlert
from strategies.macd_signal import MACDSignal
from strategies.rsi_signal import RSISignal
from strategies.kdj_signal import KDJSignal

# 策略注册表（供动态加载使用）
STRATEGY_REGISTRY = {
    "price_alert": PriceAlert,
    "volume_alert": VolumeAlert,
    "turnover_alert": TurnoverAlert,
    "macd_signal": MACDSignal,
    "rsi_signal": RSISignal,
    "kdj_signal": KDJSignal,
}

__all__ = [
    "PriceAlert", "VolumeAlert", "TurnoverAlert", "MACDSignal", "RSISignal",
    "KDJSignal", "STRATEGY_REGISTRY",
]
