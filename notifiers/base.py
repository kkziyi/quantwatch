"""
通知抽象层 — 所有通知渠道的抽象基类

定义统一的 send_alert / send_summary / is_enabled 接口。
各渠道实现（飞书、钉钉、邮件等）继承此基类。
"""

from abc import ABC, abstractmethod


class BaseNotifier(ABC):
    """通知渠道抽象基类"""

    @abstractmethod
    def send_alert(self, alert: dict) -> bool:
        """
        发送单条异动预警

        Args:
            alert: 异动字典，结构如下:
                {
                    "type": str,        # volume|turnover|macd|kdj|rsi
                    "code": str,        # 股票代码
                    "name": str,        # 股票名称
                    "direction": str,   # up|down
                    "value": float,     # 具体值
                    "threshold": float, # 触发阈值
                    "message": str,     # 已格式化的通知文本
                }

        Returns:
            True 表示发送成功
        """
        ...

    @abstractmethod
    def send_summary(self, alerts: list) -> bool:
        """
        发送汇总（日报/异动汇总）

        Args:
            alerts: 异动列表，格式与 send_alert 中的 alert 相同

        Returns:
            True 表示发送成功
        """
        ...

    @abstractmethod
    def is_enabled(self) -> bool:
        """渠道是否已配置并启用"""
        ...
