"""
通知模块 — 渠道注册 + 并行发送框架

用法:
    from notifiers import NOTIFIERS, notify_all, register_notifier
    from notifiers.feishu import FeishuNotifier

    # 注册渠道
    register_notifier("feishu", FeishuNotifier())

    # 并行发送
    notify_all(alerts)
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from notifiers.base import BaseNotifier

logger = logging.getLogger("quantwatch.notifiers")

# ═══════════════════════════════════════════════════════════════
# 渠道注册表
# ═══════════════════════════════════════════════════════════════

NOTIFIERS: dict[str, BaseNotifier] = {}


def register_notifier(name: str, notifier: BaseNotifier) -> None:
    """
    注册一个通知渠道

    Args:
        name:     渠道名称（用于日志标识）
        notifier: 实现了 BaseNotifier 的渠道实例
    """
    NOTIFIERS[name] = notifier
    logger.info(f"通知渠道已注册: {name} (enabled={notifier.is_enabled()})")


def get_enabled_notifiers() -> list:
    """返回所有已启用（is_enabled=True）的通知渠道实例"""
    return [n for n in NOTIFIERS.values() if n.is_enabled()]


# ═══════════════════════════════════════════════════════════════
# 并行发送框架
# ═══════════════════════════════════════════════════════════════

def notify_all(alerts: list, max_workers: int = 4, timeout: int = 10) -> dict:
    """
    并行通知所有已启用的渠道。

    每个启用渠道独立调用 send_alert，单个渠道失败不阻塞其他渠道。

    Args:
        alerts:      异动列表（符合 BaseNotifier.send_alert 的 alert dict 格式）
        max_workers: ThreadPoolExecutor 最大线程数
        timeout:     单个 send_alert 调用的超时秒数

    Returns:
        {"success": N, "failed": M}  — 汇总结果
    """
    enabled = get_enabled_notifiers()
    if not enabled:
        logger.debug("没有启用的通知渠道，跳过推送")
        return {"success": 0, "failed": 0}

    successes = 0
    failures = 0

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {}
        for notifier in enabled:
            for alert in alerts:
                fut = ex.submit(notifier.send_alert, alert)
                futures[fut] = (type(notifier).__name__, alert.get("name", "?"))

        for future in as_completed(futures):
            ch_name, stock_name = futures[future]
            try:
                if future.result(timeout=timeout):
                    successes += 1
                else:
                    failures += 1
            except Exception as e:
                failures += 1
                logger.warning(f"通知渠道 {ch_name} 发送失败 [{stock_name}]: {e}")

    logger.info(f"并行通知完成: {successes} 成功, {failures} 失败")
    return {"success": successes, "failed": failures}


def notify_summary(alerts: list, timeout: int = 30) -> dict:
    """
    对各启用渠道发送汇总消息。

    与 notify_all 不同，这里调用的是 send_summary（合并为一条消息），
    而非逐条 send_alert。使用 ThreadPoolExecutor 并行发送，带超时保护。

    Args:
        alerts:  异动列表（兼容新旧格式）
        timeout: 单个 send_summary 调用的超时秒数（默认 30s，因汇总消息较大）

    Returns:
        {"success": N, "failed": M}
    """
    enabled = get_enabled_notifiers()
    if not enabled:
        return {"success": 0, "failed": 0}

    successes = 0
    failures = 0

    with ThreadPoolExecutor(max_workers=len(enabled)) as ex:
        futures = {}
        for notifier in enabled:
            fut = ex.submit(notifier.send_summary, alerts)
            futures[fut] = type(notifier).__name__

        for future in as_completed(futures):
            ch_name = futures[future]
            try:
                if future.result(timeout=timeout):
                    successes += 1
                else:
                    failures += 1
            except Exception as e:
                failures += 1
                logger.warning(f"通知渠道 {ch_name} 汇总发送失败: {e}")

    logger.info(f"汇总通知完成: {successes} 成功, {failures} 失败")
    return {"success": successes, "failed": failures}
