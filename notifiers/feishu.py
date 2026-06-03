"""
飞书通知模块 — 通过 Webhook 推送股价异动预警到飞书群
"""
import json
import logging
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path

import requests

from config import (
    PROJECT_ROOT,
    FEISHU_WEBHOOK_URL,
    FEISHU_DEDUP_HOURS,
)
from notifiers.base import BaseNotifier

logger = logging.getLogger("quantwatch.feishu")

# 状态文件路径（使用 config.PROJECT_ROOT 计算，避免双重 os.path.join）
STATE_FILE = str(PROJECT_ROOT / "data" / "feishu_sent.json")

# 线程锁 — 保护 feishu_sent.json 的 Read-Modify-Write 竞态条件
_state_lock = threading.Lock()


def _load_state() -> dict:
    """加载已发送记录"""
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def _save_state(state: dict) -> None:
    """保存发送记录"""
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _should_send(stock_code: str, direction: str) -> bool:
    """
    去重检查：同股票同方向在 FEISHU_DEDUP_HOURS 小时内不重复推送

    注意：调用方必须在外层持有 _state_lock 以保证原子性。
    """
    state = _load_state()
    key = f"{stock_code}:{direction}"
    last_sent_str = state.get(key)
    if not last_sent_str:
        return True

    last_sent = datetime.fromisoformat(last_sent_str)
    cutoff = datetime.now() - timedelta(hours=FEISHU_DEDUP_HOURS)
    return last_sent < cutoff


def _mark_sent(stock_code: str, direction: str) -> None:
    """
    标记已发送

    注意：调用方必须在外层持有 _state_lock 以保证原子性。
    """
    state = _load_state()
    key = f"{stock_code}:{direction}"
    state[key] = datetime.now().isoformat()
    _save_state(state)


def _check_and_mark(stock_code: str, direction: str) -> bool:
    """
    原子操作：检查去重 + 标记已发送。

    加锁保护 Read-Modify-Write 流程，避免 notify_all() 的 ThreadPoolExecutor
    并行调用 send_alert 时出现竞态条件（两线程同时读到空状态 → 双双推送）。
    """
    with _state_lock:
        if not _should_send(stock_code, direction):
            return False
        _mark_sent(stock_code, direction)
        return True


def send_alert(stock_code: str, stock_name: str, price: float,
               change_pct: float, direction: str, is_limit: bool = False,
               open_price: float = 0, skip_dedup: bool = False) -> bool:
    """
    发送股价异动预警到飞书群

    Args:
        stock_code: 股票代码
        stock_name: 股票名称
        price: 当前价格
        change_pct: 涨跌幅（小数，如 0.035 表示 3.5%）
        direction: "up" 或 "down"
        is_limit: 是否为涨跌停触板（发送特殊卡片）
        skip_dedup: 为 True 时跳过去重检查（由上层 FeishuNotifier 自行处理去重）
    Returns:
        True 表示发送成功
    """
    if not FEISHU_WEBHOOK_URL:
        logger.warning("飞书 Webhook URL 未配置，跳过推送")
        return False

    # 去重检查（上层已做过去重时跳过，避免双重检查）
    if not skip_dedup:
        if not _check_and_mark(stock_code, direction):
            logger.debug(f"{stock_name}({stock_code}) {direction} 已在去重窗口内，跳过")
            return False
        # _check_and_mark 已在锁内完成 mark，下面只需发送即可
        already_marked = True
    else:
        already_marked = False

    if is_limit:
        # ── 涨跌停触板专用卡片 ──
        if direction == "up":
            emoji = "🔴"
            title = "涨停封板"
            color = "red"
        else:
            emoji = "🟢"
            title = "跌停封板"
            color = "green"

        card = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": f"{emoji} {title}"},
                    "template": color,
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"**{stock_name}**（{stock_code}）已触及涨跌停板",
                        },
                    },
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"当前价格：**{price:.2f}**",
                        },
                    },
                    {"tag": "hr"},
                    {
                        "tag": "note",
                        "elements": [
                            {
                                "tag": "plain_text",
                                "content": f"⏰ {datetime.now().strftime('%H:%M:%S')} ｜ QuantWatch 自动监控",
                            }
                        ],
                    },
                ],
            },
        }
    else:
        # ── 常规阈值触发卡片 ──
        emoji = "📉" if direction == "down" else "📈"
        sign = "-" if direction == "down" else "+"
        color = "red" if direction == "up" else "green"
        abs_pct = abs(change_pct) * 100

        card = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": f"{emoji} 股价异动预警"},
                    "template": color,
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"**{stock_name}**（{stock_code}）",
                        },
                    },
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"当前价格：**{price:.2f}**　涨跌幅：{sign}{abs_pct:.2f}%",
                        },
                    },
                    {"tag": "hr"},
                    {
                        "tag": "note",
                        "elements": [
                            {
                                "tag": "plain_text",
                                "content": f"⏰ {datetime.now().strftime('%H:%M:%S')} ｜ QuantWatch 自动监控",
                            }
                        ],
                    },
                ],
            },
        }

    try:
        resp = requests.post(FEISHU_WEBHOOK_URL, json=card, timeout=10)
        body = resp.json()
        if resp.status_code == 200 and body.get("code") == 0:
            if not already_marked:
                # 非 skip_dedup 路径：_check_and_mark 已标记，无需重复
                with _state_lock:
                    _mark_sent(stock_code, direction)
            logger.info(f"✅ 飞书推送成功: {stock_name}({stock_code}) {direction}"
                        f"{' (涨跌停)' if is_limit else ''}")
            return True
        else:
            logger.error(f"飞书推送失败: HTTP {resp.status_code} {body}")
            return False
    except requests.RequestException as e:
        logger.error(f"飞书推送异常: {e}")
        return False


def send_summary(alerts: list) -> bool:
    """
    发送本轮异动汇总（多只股票同时触发时合并为一条消息）

    alerts 格式: [(code, name, price, change_pct, direction, is_limit, open_price), ...]
    """
    if not alerts or not FEISHU_WEBHOOK_URL:
        return False

    # ── 批量路径中先逐条去重（原子操作）──
    filtered = []
    for alert in alerts:
        code = alert[0]
        name = alert[1]
        price = alert[2]
        pct = alert[3]
        direction = alert[4]
        is_limit = alert[5] if len(alert) >= 6 else False
        open_price = alert[6] if len(alert) >= 7 else 0

        if _check_and_mark(code, direction):
            # _check_and_mark 已在锁内完成 mark
            filtered.append((code, name, price, pct, direction, is_limit, open_price))
        else:
            logger.debug(f"{name}({code}) {direction} 已在去重窗口内，跳过批量推送")

    if not filtered:
        logger.debug("所有异动已在去重窗口内，跳过批量推送")
        return False

    if len(filtered) == 1:
        code, name, price, pct, direction, is_limit, open_px = filtered[0]
        return send_alert(code, name, price, pct, direction, is_limit, open_px)

    # ── 多条警报合并 ──
    up_count = sum(1 for _, _, _, _, d, _, _ in filtered if d == "up")
    down_count = len(filtered) - up_count

    # 确定汇总卡片标头颜色: 涨多=红, 跌多=绿, 均衡=橙
    if up_count > down_count:
        header_template = "red"
        header_prefix = "📈"
    elif down_count > up_count:
        header_template = "green"
        header_prefix = "📉"
    else:
        header_template = "orange"
        header_prefix = "📊"

    # 每只股票: 🟢📉 中国巨石(600176) **-3.17%**(加粗绿色) ↓ 开盘:42.10 收盘:42.10
    lines = []
    for code, name, price, pct, direction, is_limit, open_px in filtered:
        color_dot = "🔴" if direction == "up" else "🟢"
        emoji = "📈" if direction == "up" else "📉"
        arrow = "↑" if direction == "up" else "↓"
        sign = "+" if direction == "up" else "-"
        pct_str = f"{sign}{abs(pct)*100:.2f}%"
        open_str = f"{open_px:.2f}" if open_px else "—"
        close_str = f"{price:.2f}"

        if is_limit:
            lines.append(
                f"{color_dot} {color_dot} 涨停封板 {arrow} **{name}**({code}) **{pct_str}** {arrow} 开盘:{open_str} 收盘:**{close_str}**"
            )
        else:
            lines.append(
                f"{color_dot} {emoji} **{name}**({code}) **{pct_str}** {arrow} 开盘:{open_str} 收盘:**{close_str}**"
            )

    card = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"{header_prefix} 多股异动预警"},
                "template": header_template,
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": "\n".join(lines)},
                },
                {"tag": "hr"},
                {
                    "tag": "note",
                    "elements": [
                        {
                            "tag": "plain_text",
                            "content": f"⏰ {datetime.now().strftime('%H:%M:%S')} ｜ QuantWatch",
                        }
                    ],
                },
            ],
        },
    }

    try:
        resp = requests.post(FEISHU_WEBHOOK_URL, json=card, timeout=10)
        body = resp.json()
        if resp.status_code == 200 and body.get("code") == 0:
            logger.info(f"✅ 汇总推送成功: {len(filtered)}/{len(alerts)} 条异动"
                        f"（{len(alerts) - len(filtered)} 条被去重）")
            return True
        return False
    except requests.RequestException as e:
        logger.error(f"汇总推送异常: {e}")
        return False


# ═══════════════════════════════════════════════════════════════
# Phase 2: FeishuNotifier — 继承 BaseNotifier 的类封装
# ═══════════════════════════════════════════════════════════════

# 指标类型中文映射
_INDICATOR_LABELS = {
    "volume":   "成交量",
    "turnover": "换手率",
    "macd":     "MACD",
    "kdj":      "KDJ",
    "rsi":      "RSI",
    "price":    "股价",
}


class FeishuNotifier(BaseNotifier):
    """飞书通知渠道 — 封装 Webhook 推送"""

    def is_enabled(self) -> bool:
        """飞书渠道是否已配置 Webhook"""
        return bool(FEISHU_WEBHOOK_URL)

    # ── send_alert: 新指标格式 ────────────────────────────
    def send_alert(self, alert: dict) -> bool:
        """
        发送单条指标异动预警（新格式）

        兼容 Phase 1 旧格式（含 price/change_pct）：
        - 旧格式直接调用模块级 send_alert 函数（skip_dedup=True，因本方法已做去重）
        - 新格式构建通用指标卡片
        """
        if not FEISHU_WEBHOOK_URL:
            logger.warning("飞书 Webhook URL 未配置，跳过推送")
            return False

        code = alert.get("code", "")
        name = alert.get("name", "")
        direction = alert.get("direction", "up")

        # 去重检查（原子操作）
        if not _check_and_mark(code, direction):
            logger.debug(f"{name}({code}) {direction} 已在去重窗口内，跳过")
            return False

        # ── 兼容 Phase 1 旧格式：有 price 字段则走旧逻辑 ──
        # 传 skip_dedup=True 避免模块级 send_alert 二次去重
        if "price" in alert:
            return send_alert(
                stock_code=code,
                stock_name=name,
                price=alert.get("price", 0),
                change_pct=alert.get("change_pct", 0),
                direction=direction,
                is_limit=alert.get("is_limit", False),
                open_price=alert.get("open_price", 0),
                skip_dedup=True,
            )

        # ── 新格式：通用指标卡片 ──
        indicator_type = alert.get("type", "unknown")
        indicator_label = _INDICATOR_LABELS.get(indicator_type, indicator_type)
        value = alert.get("value", "")
        threshold = alert.get("threshold", "")
        message = alert.get("message", "")

        emoji = "📈" if direction == "up" else "📉"
        color = "red" if direction == "up" else "green"
        title = "指标异动预警"

        # 卡片内容
        content_parts = [
            f"**{name}**（{code}）",
        ]
        if message:
            content_parts.append(message)
        else:
            content_parts.append(f"{indicator_label}异动: **{value}**（阈值: {threshold}）")

        card = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": f"{emoji} {title}"},
                    "template": color,
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": "\n".join(content_parts),
                        },
                    },
                    {"tag": "hr"},
                    {
                        "tag": "note",
                        "elements": [
                            {
                                "tag": "plain_text",
                                "content": f"⏰ {datetime.now().strftime('%H:%M:%S')} ｜ QuantWatch 自动监控",
                            }
                        ],
                    },
                ],
            },
        }

        try:
            resp = requests.post(FEISHU_WEBHOOK_URL, json=card, timeout=10)
            body = resp.json()
            if resp.status_code == 200 and body.get("code") == 0:
                logger.info(f"✅ 飞书推送成功: {name}({code}) {indicator_label} {direction}")
                return True
            else:
                logger.error(f"飞书推送失败: HTTP {resp.status_code} {body}")
                return False
        except requests.RequestException as e:
            logger.error(f"飞书推送异常: {e}")
            return False

    # ── send_summary: 新格式 → 旧格式桥接 ─────────────────
    def send_summary(self, alerts: list) -> bool:
        """
        发送异动汇总（新格式 alerts → 旧格式 send_summary）

        自动检测 alert 格式：
        - 列表元素为 tuple → 直接调用旧 send_summary
        - 列表元素为 dict  → 转换为 tuple 再调用
        """
        if not alerts or not FEISHU_WEBHOOK_URL:
            return False

        # 检测格式
        if alerts and isinstance(alerts[0], dict):
            # 新格式 dict → 旧格式 tuple
            # 注意：price 值优先取 price 字段（股价异动），非股价指标（volume/macd/rsi）
            # 使用 value 字段作为展示值，不再统一渲染为 price
            legacy_alerts = []
            for a in alerts:
                legacy_alerts.append((
                    a.get("code", ""),
                    a.get("name", ""),
                    a.get("price", a.get("value", 0)),
                    a.get("change_pct", 0),
                    a.get("direction", "up"),
                    a.get("is_limit", False),
                    a.get("open_price", 0),
                ))
            return send_summary(legacy_alerts)
        else:
            # 旧格式 tuple 直接透传
            return send_summary(alerts)
