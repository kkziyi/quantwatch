"""
盘中扫描 + 买入信号推送 — 每 15 分钟全市场扫描，触发买入信号推送到飞书

用法:
    from reports.intraday_scanner import run_intraday_scan

    # 在主循环中每次 poll 时调用（内部自动管理 15 分钟间隔）
    n = run_intraday_scan()

过滤规则:
    1. 开市 30 分钟内静默（不推送）
    2. 同一股票每天只推送一次
    3. 同一板块 15 分钟内不重复推送
    4. 每日最多推送 3 条

推送内容:
    当前价位、涨幅、量比、距涨停空间、触发规则、操作建议
"""

import json
import logging
import os
import sys
import threading
from datetime import datetime, date, time as dt_time
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

# 确保项目根目录在 sys.path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import PROJECT_ROOT, FEISHU_WEBHOOK_URL, TRADING_START
from reports.screener import ScreenerEngine
from strategies.price_alert import PriceAlert

logger = logging.getLogger("quantwatch.intraday_scanner")

# ═══════════════════════════════════════════════════════════════
# 板块检测与涨跌幅限制
# ═══════════════════════════════════════════════════════════════

_BOARD_PREFIX = [
    ("688", "科创板"),
    ("300", "创业板"),
    ("301", "创业板"),
    ("920", "北交所"),
    ("8",   "北交所"),
    ("4",   "北交所"),
    ("60",  "SH主板"),
    ("002", "SZ中小板"),
    ("003", "SZ主板"),
    ("00",  "SZ主板"),
]

_LIMIT_UP = {
    "SH主板": 10.0,
    "SZ主板": 10.0,
    "SZ中小板": 10.0,
    "创业板": 20.0,
    "科创板": 20.0,
    "北交所": 30.0,
}


def detect_board(code: str) -> str:
    """从股票代码识别所属板块"""
    code = str(code).zfill(6)
    if not code or code == "000000":
        return "未知"
    for prefix, board in _BOARD_PREFIX:
        if code.startswith(prefix):
            return board
    return "未知"


def limit_up_pct(code: str) -> float:
    """获取该股票的涨停幅度（%）"""
    return _LIMIT_UP.get(detect_board(code), 10.0)


# ═══════════════════════════════════════════════════════════════
# 状态持久化
# ═══════════════════════════════════════════════════════════════

_STATE_FILE = str(PROJECT_ROOT / "data" / "intraday_scanner_state.json")
_STATE_LOCK = threading.Lock()


def _load_state() -> dict:
    if not os.path.exists(_STATE_FILE):
        return {"date": "", "sent_stocks": [], "board_cooldowns": {}, "daily_count": 0}
    try:
        with open(_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {"date": "", "sent_stocks": [], "board_cooldowns": {}, "daily_count": 0}


def _save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(_STATE_FILE), exist_ok=True)
    with open(_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _clear_intraday_state() -> None:
    """删除盘中扫描状态文件（用于测试重置）"""
    if os.path.exists(_STATE_FILE):
        os.remove(_STATE_FILE)


# ═══════════════════════════════════════════════════════════════
# IntradayScanner
# ═══════════════════════════════════════════════════════════════

class IntradayScanner:
    """盘中全市场扫描 + 买入信号推送

    每 15 分钟通过 ScreenerEngine 扫描全市场，
    触发买入规则时推送飞书卡片。内置多层过滤避免骚扰。

    Attributes:
        SCAN_INTERVAL: 扫描间隔（秒），默认 900（15 分钟）
        QUIET_MINUTES: 开市后静默时长（分钟），默认 30
        BOARD_COOLDOWN: 同板块冷却间隔（秒），默认 900
        MAX_DAILY_PUSHES: 每日最大推送数，默认 3
    """

    SCAN_INTERVAL = 900          # 15 分钟
    QUIET_MINUTES = 30           # 开市后 30 分钟不推送
    BOARD_COOLDOWN = 900         # 同板块 15 分钟冷却
    MAX_DAILY_PUSHES = 3         # 每日上限

    def __init__(self):
        self._last_scan: Optional[datetime] = None
        self._engine: Optional[ScreenerEngine] = None
        self._today = date.today()
        self._reset_daily_state()

    # ── 引擎懒加载 ──────────────────────────────────────────

    def _get_engine(self) -> ScreenerEngine:
        """懒加载 ScreenerEngine，每次扫描前清除缓存以获取实时数据"""
        if self._engine is None:
            self._engine = ScreenerEngine()
        # 清除市场数据缓存，确保每次扫描拉取最新行情
        self._engine._market_data = None
        return self._engine

    # ── 每日状态重置 ────────────────────────────────────────

    def _reset_daily_state(self) -> None:
        """跨天自动重置推送状态"""
        with _STATE_LOCK:
            state = _load_state()
            if state.get("date") != str(self._today):
                _save_state({
                    "date": str(self._today),
                    "sent_stocks": [],
                    "board_cooldowns": {},
                    "daily_count": 0,
                })
                logger.info("盘中扫描: 新的一天，状态已重置")

    # ── 时间判断 ────────────────────────────────────────────

    def should_scan(self) -> bool:
        """是否到了扫描时间点（距离上次扫描 >= 15 分钟）"""
        if self._last_scan is None:
            return True
        return (datetime.now() - self._last_scan).total_seconds() >= self.SCAN_INTERVAL

    def _is_trading_time(self) -> bool:
        """是否为 A 股盘中交易时段"""
        now = datetime.now()
        if not PriceAlert.is_trading_day(now):
            return False
        return PriceAlert.is_trading_time(now)

    def _is_quiet_period(self) -> bool:
        """开市后 QUIET_MINUTES 分钟内为静默期"""
        now = datetime.now()
        try:
            start = dt_time.fromisoformat(TRADING_START)
        except ValueError:
            return False
        quiet_end_hour = (start.hour * 60 + start.minute + self.QUIET_MINUTES) // 60
        quiet_end_min = (start.hour * 60 + start.minute + self.QUIET_MINUTES) % 60
        quiet_end = dt_time(quiet_end_hour, quiet_end_min)
        return now.time() <= quiet_end

    # ── 主扫描入口 ──────────────────────────────────────────

    def scan_and_push(self) -> int:
        """执行一次扫描周期。返回成功推送的信号数。"""
        # 非交易时间不扫描
        if not self._is_trading_time():
            return 0

        # 未到扫描时间间隔
        if not self.should_scan():
            return 0

        # 跨天重置
        if self._today != date.today():
            self._today = date.today()
            self._reset_daily_state()

        # 静默期只记录日志，不重置 _last_scan
        if self._is_quiet_period():
            logger.debug(
                f"盘中扫描: 开市 {self.QUIET_MINUTES} 分钟静默期，跳过推送"
            )
            return 0

        # 检查今日上限
        with _STATE_LOCK:
            state = _load_state()
            if state.get("daily_count", 0) >= self.MAX_DAILY_PUSHES:
                logger.debug(
                    f"盘中扫描: 今日已达上限 {self.MAX_DAILY_PUSHES} 条，跳过"
                )
                self._last_scan = datetime.now()
                return 0

        # 调用选股引擎
        logger.info("盘中扫描: 运行选股引擎 (volume_breakout + limit_up_analysis) ...")
        try:
            engine = self._get_engine()
            results = engine.screen(rules=["volume_breakout", "limit_up_analysis"])
        except Exception as e:
            logger.error(f"盘中扫描: 选股引擎执行失败: {e}", exc_info=True)
            self._last_scan = datetime.now()
            return 0

        # 合并结果并构建候选列表
        candidates = self._build_candidates(results)
        if not candidates:
            logger.info("盘中扫描: 无候选信号")
            self._last_scan = datetime.now()
            return 0

        # 应用过滤规则并推送
        pushed = self._apply_filters_and_push(candidates)
        self._last_scan = datetime.now()
        logger.info(
            f"盘中扫描: 本轮候选 {len(candidates)} 只，推送 {pushed} 条"
        )
        return pushed

    # ── 候选构建 ────────────────────────────────────────────

    def _build_candidates(self, results: dict) -> list:
        """将 screener 结果合并为统一候选列表，按涨幅降序"""
        seen = set()
        candidates = []

        for rule, df in results.items():
            if df is None or (hasattr(df, 'empty') and df.empty):
                continue
            for _, row in df.iterrows():
                code = str(row.get("code", "")).strip()
                if not code or code in seen:
                    continue
                seen.add(code)

                candidates.append({
                    "code": code,
                    "name": str(row.get("name", code)),
                    "price": _safe_float(row.get("price")),
                    "change_pct": _safe_float(row.get("change_pct")),
                    "volume_ratio": _safe_float(row.get("volume_ratio")),
                    "turnover": _safe_float(row.get("turnover")),
                    "total_mcap": _safe_float(row.get("total_mcap")),
                    "reason": str(row.get("reason", "")),
                    "rule": rule,
                })

        candidates.sort(key=lambda x: x["change_pct"], reverse=True)
        return candidates

    # ── 过滤 + 推送 ─────────────────────────────────────────

    def _apply_filters_and_push(self, candidates: list) -> int:
        """应用 4 层过滤规则，逐条推送"""
        now = datetime.now()
        pushed = 0

        with _STATE_LOCK:
            state = _load_state()
            sent = set(state.get("sent_stocks", []))
            board_cooldowns = state.get("board_cooldowns", {})
            daily_count = state.get("daily_count", 0)

            for c in candidates:
                if daily_count >= self.MAX_DAILY_PUSHES:
                    break

                # ── 规则 2: 同股票一天一次 ──
                if c["code"] in sent:
                    continue

                # ── 规则 3: 同板块 15 分钟冷却 ──
                board = detect_board(c["code"])
                last_time_str = board_cooldowns.get(board, "")
                if last_time_str:
                    try:
                        last_time = datetime.fromisoformat(last_time_str)
                        if (now - last_time).total_seconds() < self.BOARD_COOLDOWN:
                            logger.debug(
                                f"盘中扫描: {c['name']} 板块 {board} 冷却中，跳过"
                            )
                            continue
                    except ValueError:
                        pass

                # ── 推送 ──
                success = self._push_signal(c)
                if success:
                    sent.add(c["code"])
                    board_cooldowns[board] = now.isoformat()
                    daily_count += 1
                    pushed += 1
                    logger.info(
                        f"盘中扫描: 推送 #{daily_count} — "
                        f"{c['name']}({c['code']}) {c['reason']}"
                    )

            # 保存状态
            _save_state({
                "date": str(self._today),
                "sent_stocks": list(sent),
                "board_cooldowns": board_cooldowns,
                "daily_count": daily_count,
            })

        return pushed

    # ── 飞书推送 ────────────────────────────────────────────

    def _push_signal(self, c: dict) -> bool:
        """构造飞书卡片并推送买入信号"""
        if not FEISHU_WEBHOOK_URL:
            logger.warning("飞书 Webhook 未配置，跳过推送")
            return False

        code = c["code"]
        name = c["name"]
        price = c["price"]
        change_pct = c["change_pct"]
        volume_ratio = c["volume_ratio"]
        turnover = c["turnover"]
        reason = c["reason"]
        rule = c["rule"]
        mcap = c["total_mcap"]

        sign = "+" if change_pct > 0 else ""
        now_str = datetime.now().strftime("%H:%M:%S")
        board = detect_board(code)

        # 距涨停空间
        lu = limit_up_pct(code)
        dist_str = f"{(lu - change_pct):.1f}%"
        if change_pct >= lu - 0.5:
            dist_str = "⏳ 即将封板"

        # 规则标签
        rule_label = {
            "volume_breakout": "放量突破",
            "limit_up_analysis": "涨停分析",
            "ma_bullish_alignment": "均线多头",
            "macd_golden_cross": "MACD金叉",
        }.get(rule, rule)

        # 操作建议
        suggestion = self._make_suggestion(change_pct, volume_ratio, turnover)

        # 板块标签
        board_tag = f"🏷 {board}"

        card = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": "🔔 盘中买入信号"},
                    "template": "red",
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": (
                                f"**{name}**（{code}）{sign}{change_pct:.2f}%\n"
                                f"💰 当前价 **{price:.2f}** ｜ "
                                f"量比 **{volume_ratio:.1f}** ｜ "
                                f"换手 **{turnover:.1f}%**\n"
                                f"📏 距涨停 **{dist_str}** ｜ {board_tag}"
                                + (f" ｜ 市值 **{mcap/1e8:.0f}亿**" if mcap > 0 else "")
                            ),
                        },
                    },
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"🎯 触发规则：**{rule_label}**\n📋 {reason}",
                        },
                    },
                    {"tag": "hr"},
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"💡 建议：{suggestion}",
                        },
                    },
                    {"tag": "hr"},
                    {
                        "tag": "note",
                        "elements": [
                            {
                                "tag": "plain_text",
                                "content": (
                                    f"⏰ {now_str} ｜ "
                                    f"今日第 {self.MAX_DAILY_PUSHES} 条限额 ｜ "
                                    "QuantWatch 盘中扫描"
                                ),
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
                logger.info(f"✅ 盘中信号推送: {name}({code})")
                return True
            else:
                logger.error(f"盘中信号推送失败 ({resp.status_code}): {body}")
                return False
        except requests.RequestException as e:
            logger.error(f"盘中信号推送异常: {e}")
            return False

    @staticmethod
    def _make_suggestion(change_pct: float, volume_ratio: float,
                         turnover: float) -> str:
        """根据涨幅和量比生成操作建议"""
        if change_pct > 7:
            return "⚠️ 涨幅已较高，追涨风险较大，建议等待回调或分批建仓"
        elif change_pct > 5:
            if volume_ratio > 3:
                return "🔥 强势突破，量价配合良好，可考虑跟随"
            else:
                return "📈 涨幅可观但量能一般，小仓位试探，严格止损"
        elif change_pct > 3:
            if volume_ratio > 2:
                return "📊 放量启动信号，趋势确认中，可小仓位参与"
            else:
                return "👀 温和上涨，关注后续量能确认"
        else:
            return "👀 关注，等待进一步确认信号或回调介入"


# ═══════════════════════════════════════════════════════════════
# 便捷入口（供 main.py 使用）
# ═══════════════════════════════════════════════════════════════

_scanner: Optional[IntradayScanner] = None


def run_intraday_scan() -> int:
    """模块级便捷入口：执行一次盘中扫描周期

    在主循环每次轮询时调用，内部自动管理 15 分钟间隔、
    静默期、非交易时段等判断，安全无副作用。

    Returns:
        本轮推送的信号数量（0 表示无推送或未到扫描时间）
    """
    global _scanner
    if _scanner is None:
        _scanner = IntradayScanner()
    try:
        return _scanner.scan_and_push()
    except Exception as e:
        logger.error(f"盘中扫描异常: {e}", exc_info=True)
        return 0


def reset_scanner() -> None:
    """重置扫描器状态（测试用）"""
    global _scanner
    _scanner = None
    _clear_intraday_state()


# ═══════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════

def _safe_float(val) -> float:
    """安全转换为 float，异常时返回 0"""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0
