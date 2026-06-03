"""
收盘后调度框架 — 支持注册多个盘后任务（MACD/KDJ/RSI 等），
交易结束后依次执行，同一天不重复运行。
"""

from schedulers.after_close import AfterCloseScheduler

__all__ = ["AfterCloseScheduler"]
