"""Portfolio — 组合管理框架

提供多组合持仓管理：增删改查、JSON 持久化、CSV 导入，
盘中盈亏监控、止损/止盈检测，以及收盘后组合日报推送。
"""

from .manager import PortfolioManager
from .tracker import PortfolioTracker
from .daily_report import PortfolioDailyReporter, run_portfolio_daily

__all__ = [
    "PortfolioManager",
    "PortfolioTracker",
    "PortfolioDailyReporter",
    "run_portfolio_daily",
]
