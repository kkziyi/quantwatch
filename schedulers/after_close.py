"""
收盘后调度框架 — 注册的收盘后任务在交易结束后依次执行

用法:
    from schedulers import AfterCloseScheduler

    _after_close = AfterCloseScheduler()

    def _run_macd():
        signals = check_macd_signals()
        if signals:
            return send_macd_alerts(signals)
        return 0

    _after_close.register("macd", _run_macd, order=10)
"""

import logging
from datetime import date, datetime

logger = logging.getLogger("quantwatch.after_close")


class AfterCloseScheduler:
    """收盘后调度框架

    - 注册的任务在交易结束后依次执行
    - 同一任务同一天只执行一次（基于日期去重）
    - 单个任务失败不影响其他任务
    - order 越小越先执行
    """

    def __init__(self):
        self._tasks: dict[str, callable] = {}    # {name: callable}
        self._checked: dict[str, date] = {}      # {name: date} 记录哪天已执行
        self._order: list[tuple[int, str]] = []  # [(order, name), ...]

    def register(self, name: str, fn: callable, order: int = 100):
        """注册收盘后任务

        Args:
            name: 任务名称（唯一标识）
            fn: 任务函数，无参，返回值作为 result 记录
            order: 执行顺序，越小越先执行（默认 100）
        """
        self._tasks[name] = fn
        self._order.append((order, name))
        self._order.sort()

    def run_pending(self, today: date = None) -> list:
        """运行所有今日未执行的任务

        任务内部应自己处理异常（不要抛到外面）。
        这里仍然兜底捕获，确保单个失败不阻塞其他任务。

        Args:
            today: 指定日期（默认今天）

        Returns:
            [(name, result), ...]
        """
        today = today or datetime.now().date()
        results = []

        for _, name in self._order:
            if self._checked.get(name) != today:
                try:
                    result = self._tasks[name]()
                    self._checked[name] = today
                    results.append((name, result))
                except Exception as e:
                    logger.warning(f"收盘后任务 [{name}] 执行失败: {e}")

        return results

    def mark_done(self, name: str, day: date = None):
        """手动标记某任务今日已执行（用于单次测试跳过）"""
        self._checked[name] = day or datetime.now().date()

    def reset(self):
        """清空执行记录（跨天时自动重置）"""
        self._checked = {}
