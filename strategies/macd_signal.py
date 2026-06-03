"""
MACD 金叉/死叉信号检测模块 — 收盘后执行，检测 MACD(12,26,9) 金叉/死叉并推送飞书

Phase 2a: MACDSignal 类实现，含 MACD 计算、DIFF_GAP 过滤、
零轴区分（金叉/死叉）、日期级去重推送。
"""
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import requests

# 确保项目根目录在 sys.path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import (
    STOCKS, STRATEGIES,
    FEISHU_WEBHOOK_URL,
    AKSHARE_DELAY,
)

logger = logging.getLogger("quantwatch.macd_signal")


class MACDSignal:
    """
    MACD 金叉/死叉信号检测器。

    收盘后执行（非盘中轮询）。
    使用 AKShare stock_zh_a_hist(adjust="qfq") 获取前复权日线数据，
    计算 MACD(12,26,9)，检测金叉/死叉信号。

    特性：
    - 交叉日当天识别即报告（不延迟确认）
    - DIFF_GAP 过滤：|DIF - DEA| > max(close × 0.005, 0.03) 才算真信号
    - 零轴区分：零上金叉（强势）/ 零下金叉（反弹）；零上死叉（见顶）/ 零下死叉（弱势）
    - 去重：key 格式 macd:{signal_type}:{code}:{date}，按日期区分事件型信号
    """

    def __init__(self, config=None):
        if config is None:
            config = STRATEGIES.get("macd_signal", {})
        self.enabled = config.get("enabled", False)
        self.fast = config.get("fast", 12)
        self.slow = config.get("slow", 26)
        self.signal_period = config.get("signal", 9)
        self.distinguish_zero_cross = config.get("distinguish_zero_cross", True)
        self.diff_gap_method = config.get("diff_gap_method", "ratio")
        self.diff_gap_ratio = config.get("diff_gap_ratio", 0.005)
        self.diff_gap_min = config.get("diff_gap_min", 0.03)

        # 数据缓存（同一天内复用）
        self._history_cache = {}       # {code: DataFrame}
        self._cache_date = None        # 缓存日期

    # ── 状态文件 ──────────────────────────────────────────────

    @staticmethod
    def _state_file_path():
        """获取去重状态文件路径"""
        return os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data", "feishu_sent.json",
        )

    # ── 数据获取 ──────────────────────────────────────────────

    def _fetch_history(self, code):
        """获取单只股票近 ~3 个月前复权日线数据

        Args:
            code: 股票代码（如 "600176"）

        Returns:
            DataFrame，columns 包含 日期, 开盘, 收盘, 最高, 最低, 成交量
            失败返回空 DataFrame
        """
        try:
            import akshare as ak
        except ImportError:
            logger.error("akshare 未安装，无法获取历史数据")
            return pd.DataFrame()

        # 取 90 天数据，确保有足够交易日计算 MACD（至少需要 26+9=35 个交易日）
        start_date = (datetime.now() - timedelta(days=120)).strftime("%Y%m%d")
        end_date = datetime.now().strftime("%Y%m%d")

        try:
            df = ak.stock_zh_a_hist(
                symbol=code,
                period="daily",
                start_date=start_date,
                end_date=end_date,
                adjust="qfq",  # 前复权
            )

            if df is None or len(df) == 0:
                logger.warning(f"  {code}: 无历史数据")
                return pd.DataFrame()

            # 标准化列名
            col_map = {
                "日期": "date",
                "开盘": "open",
                "收盘": "close",
                "最高": "high",
                "最低": "low",
                "成交量": "volume",
            }
            # 保留原始中文列名，也允许英文访问
            for cn, en in col_map.items():
                if cn in df.columns:
                    df[en] = df[cn]

            # 确保按日期升序排列
            if "date" in df.columns:
                df["date"] = pd.to_datetime(df["date"])
                df = df.sort_values("date").reset_index(drop=True)

            # 过滤掉停牌日（收盘价 = 0 或 NaN）
            if "close" in df.columns:
                df = df[df["close"].notna() & (df["close"] > 0)]

            return df

        except Exception as e:
            logger.warning(f"  {code}: 获取历史数据失败: {e}")
            return pd.DataFrame()

    def _get_history(self, code):
        """获取历史数据（带同一天缓存）

        同一天内多次调用同一股票复用缓存，跨天自动刷新。
        """
        today = datetime.now().date()
        if self._cache_date != today:
            self._history_cache = {}
            self._cache_date = today

        if code not in self._history_cache:
            df = self._fetch_history(code)
            self._history_cache[code] = df
            # AKShare 请求间隔
            time.sleep(AKSHARE_DELAY)

        return self._history_cache[code]

    # ── MACD 计算 ─────────────────────────────────────────────

    @staticmethod
    def _compute_ema(series, period):
        """计算指数移动平均（EMA）

        Args:
            series: pandas Series
            period: EMA 周期

        Returns:
            pandas Series
        """
        return series.ewm(span=period, adjust=False).mean()

    def compute_macd(self, df):
        """计算 MACD(12,26,9)

        Args:
            df: DataFrame，需包含 'close' 列

        Returns:
            DataFrame，新增 DIF, DEA, MACD_hist 列
        """
        close = df["close"].astype(float)

        ema_fast = self._compute_ema(close, self.fast)
        ema_slow = self._compute_ema(close, self.slow)

        dif = ema_fast - ema_slow
        dea = self._compute_ema(dif, self.signal_period)
        macd_hist = 2 * (dif - dea)

        result = df.copy()
        result["DIF"] = dif
        result["DEA"] = dea
        result["MACD_hist"] = macd_hist

        return result

    # ── 信号检测 ──────────────────────────────────────────────

    def _check_gap_filter(self, dif_val, dea_val, close_val):
        """DIFF_GAP 过滤检测

        |DIF - DEA| > max(close × diff_gap_ratio, diff_gap_min) 才算真信号

        Returns:
            True 表示通过过滤（是真信号），False 表示不通过
        """
        gap = abs(dif_val - dea_val)
        threshold = max(close_val * self.diff_gap_ratio, self.diff_gap_min or 0.03)
        return gap > threshold

    def check_signals(self, stock_codes=None):
        """主检测入口：遍历自选股，检测 MACD 金叉/死叉信号

        Args:
            stock_codes: 股票代码列表，默认使用 config.STOCKS

        Returns:
            list of dict:
                {
                    "code": "600176",
                    "name": "中国巨石",
                    "type": "golden_cross" / "dead_cross",
                    "label": "零上金叉（强势）" / "零下金叉（反弹）" / "金叉" / "死叉",
                    "DIF": 0.87,
                    "DEA": 0.62,
                    "gap": 0.25,
                    "close": 15.23,
                    "date": "2026-05-29",
                }
        """
        if not self.enabled:
            logger.info("MACD 信号检测未启用，跳过")
            return []

        if stock_codes is None:
            stock_codes = list(STOCKS.keys())

        signals = []
        logger.info(f"开始 MACD 信号检测，共 {len(stock_codes)} 只股票...")

        for code in stock_codes:
            try:
                signal = self._check_single(code)
                if signal:
                    signals.append(signal)
            except Exception as e:
                logger.warning(f"  {code}: MACD 检测失败: {e}")

            # AKShare 请求间隔
            time.sleep(AKSHARE_DELAY)

        logger.info(f"MACD 信号检测完成: 共 {len(signals)} 个信号")
        return signals

    def _check_single(self, code):
        """检测单只股票的 MACD 金叉/死叉

        Args:
            code: 股票代码

        Returns:
            dict 或 None
        """
        name = STOCKS.get(code, {}).get("name", code)

        # 获取历史数据
        df = self._get_history(code)
        if df is None or len(df) == 0:
            logger.warning(f"  {code}({name}): 无历史数据，跳过")
            return None

        # 检查数据量：至少需要 slow+signal_period 个交易日
        min_days = self.slow + self.signal_period  # 26+9=35
        if len(df) < min_days:
            logger.info(
                f"  {code}({name}): 数据不足 {min_days} 个交易日（仅 {len(df)} 天），跳过"
            )
            return None

        # 计算 MACD
        df = self.compute_macd(df)

        # 取最近的数据点
        if len(df) < 3:
            return None

        # 最近两天（T-1 = 昨天, T = 今天/最近一天）
        t_row = df.iloc[-1]    # 最近一天
        t1_row = df.iloc[-2]   # 倒数第二天

        dif_t = float(t_row["DIF"])
        dea_t = float(t_row["DEA"])
        dif_t1 = float(t1_row["DIF"])
        dea_t1 = float(t1_row["DEA"])
        close_t = float(t_row["close"])
        date_t = str(t_row.get("date", ""))[:10] if "date" in df.columns else ""

        # 检查是否有 NaN
        if pd.isna(dif_t) or pd.isna(dea_t) or pd.isna(dif_t1) or pd.isna(dea_t1):
            logger.info(f"  {code}({name}): MACD 数据含 NaN，跳过")
            return None

        # ── 金叉检测 ──
        # 条件：DIF[t-1] <= DEA[t-1] AND DIF[t] > DEA[t]（交叉当天）
        if dif_t1 <= dea_t1 and dif_t > dea_t:
            # DIFF_GAP 过滤
            if not self._check_gap_filter(dif_t, dea_t, close_t):
                logger.debug(
                    f"  {code}({name}): 疑似金叉但 DIFF_GAP 过小 "
                    f"(DIF={dif_t:.4f}, DEA={dea_t:.4f}, close={close_t:.2f})，跳过"
                )
                return None

            # 零轴区分
            if self.distinguish_zero_cross:
                if dif_t > 0 and dea_t > 0:
                    label = "零上金叉（强势）"
                    emoji = "🟢"
                elif dif_t < 0 and dea_t < 0:
                    label = "零下金叉（反弹）"
                    emoji = "🟡"
                else:
                    label = "金叉"
                    emoji = "🟢"
            else:
                label = "金叉"
                emoji = "🟢"

            return {
                "code": code,
                "name": name,
                "type": "golden_cross",
                "label": label,
                "emoji": emoji,
                "DIF": round(dif_t, 4),
                "DEA": round(dea_t, 4),
                "gap": round(dif_t - dea_t, 4),
                "close": round(close_t, 2),
                "date": date_t,
            }

        # ── 死叉检测 ──
        # 条件：DIF[t-1] >= DEA[t-1] AND DIF[t] < DEA[t]（交叉当天）
        if dif_t1 >= dea_t1 and dif_t < dea_t:
            # DIFF_GAP 过滤
            if not self._check_gap_filter(dif_t, dea_t, close_t):
                logger.debug(
                    f"  {code}({name}): 疑似死叉但 DIFF_GAP 过小 "
                    f"(DIF={dif_t:.4f}, DEA={dea_t:.4f}, close={close_t:.2f})，跳过"
                )
                return None

            # 零轴区分（与金叉对称）
            if self.distinguish_zero_cross:
                if dif_t > 0 and dea_t > 0:
                    label = "零上死叉（见顶）"
                    emoji = "🔴"
                elif dif_t < 0 and dea_t < 0:
                    label = "零下死叉（弱势）"
                    emoji = "🔴"
                else:
                    label = "死叉"
                    emoji = "🔴"
            else:
                label = "死叉"
                emoji = "🔴"

            return {
                "code": code,
                "name": name,
                "type": "dead_cross",
                "label": label,
                "emoji": emoji,
                "DIF": round(dif_t, 4),
                "DEA": round(dea_t, 4),
                "gap": round(dif_t - dea_t, 4),
                "close": round(close_t, 2),
                "date": date_t,
            }

        return None

    # ── 去重 ──────────────────────────────────────────────────

    def _should_send_macd(self, code, signal_type, date_str):
        """去重检查：同股票同信号类型同日期内不重复推送

        Key 格式:
            macd:golden_cross:{code}:{date_str}
            macd:dead_cross:{code}:{date_str}

        Args:
            code: 股票代码
            signal_type: "golden_cross" / "dead_cross"
            date_str: 信号日期（如 "2026-05-31"）

        Returns:
            True 表示应该发送
        """
        state_file = self._state_file_path()
        if not os.path.exists(state_file):
            return True

        try:
            with open(state_file, "r", encoding="utf-8") as f:
                state = json.load(f)
        except (json.JSONDecodeError, IOError):
            return True

        key = f"macd:{signal_type}:{code}:{date_str}"
        return key not in state

    def _mark_sent_macd(self, code, signal_type, date_str):
        """标记 MACD 信号已发送"""
        state_file = self._state_file_path()
        state = {}

        if os.path.exists(state_file):
            try:
                with open(state_file, "r", encoding="utf-8") as f:
                    state = json.load(f)
            except (json.JSONDecodeError, IOError):
                pass

        key = f"macd:{signal_type}:{code}:{date_str}"
        state[key] = datetime.now().isoformat()

        os.makedirs(os.path.dirname(state_file), exist_ok=True)
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)

    # ── 飞书推送 ──────────────────────────────────────────────

    def send_macd_alert(self, signal):
        """发送单条 MACD 信号飞书卡片通知

        Args:
            signal: check_signals() 返回的 dict

        Returns:
            True 表示发送成功
        """
        if not FEISHU_WEBHOOK_URL:
            logger.warning("飞书 Webhook URL 未配置，跳过 MACD 推送")
            return False

        code = signal["code"]
        name = signal["name"]
        signal_type = signal["type"]
        label = signal["label"]
        emoji = signal["emoji"]
        dif_val = signal["DIF"]
        dea_val = signal["DEA"]
        gap = signal["gap"]
        close_val = signal["close"]

        # 去重检查
        if not self._should_send_macd(code, signal_type, signal.get("date", "")):
            logger.debug(f"{name}({code}) MACD {label} 已在去重窗口内，跳过")
            return False

        # 构建卡片
        card = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": f"{emoji} MACD {label}"},
                    "template": "red" if signal_type == "dead_cross" else "green",
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": (
                                f"**{name}**（{code}）\\n"
                                f"DIF=**{dif_val:.4f}**　DEA=**{dea_val:.4f}**　"
                                f"差值=**{gap:+.4f}**　收盘={close_val:.2f}"
                            ),
                        },
                    },
                    {"tag": "hr"},
                    {
                        "tag": "note",
                        "elements": [
                            {
                                "tag": "plain_text",
                                "content": (
                                    f"⏰ {datetime.now().strftime('%H:%M:%S')} ｜ "
                                    f"QuantWatch 自动监控（盘后）"
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
                self._mark_sent_macd(code, signal_type, signal.get("date", ""))
                logger.info(
                    f"✅ MACD 飞书推送成功: {name}({code}) {label} "
                    f"DIF={dif_val:.4f} DEA={dea_val:.4f}"
                )
                return True
            else:
                logger.error(f"MACD 飞书推送失败: HTTP {resp.status_code} {body}")
                return False
        except requests.RequestException as e:
            logger.error(f"MACD 飞书推送异常: {e}")
            return False

    def send_macd_alerts(self, signals):
        """批量发送 MACD 信号

        Args:
            signals: check_signals() 返回的列表

        Returns:
            实际发送条数
        """
        if not signals or not FEISHU_WEBHOOK_URL:
            return 0

        sent_count = 0
        for signal in signals:
            if self.send_macd_alert(signal):
                sent_count += 1
                time.sleep(0.5)  # 飞书 Webhook 限速

        return sent_count

    # ── 主入口（收盘后调用） ──────────────────────────────────

    def run(self, stock_codes=None):
        """收盘后主入口：检测 MACD 信号并推送

        Args:
            stock_codes: 股票代码列表，默认使用 config.STOCKS

        Returns:
            (检测信号数, 推送成功数)
        """
        signals = self.check_signals(stock_codes)

        # 日志输出
        for sig in signals:
            logger.info(
                f"  {sig['emoji']} {sig['label']}: {sig['name']}({sig['code']}) "
                f"DIF={sig['DIF']:.4f} DEA={sig['DEA']:.4f} 差值={sig['gap']:+.4f}"
            )

        sent = self.send_macd_alerts(signals)
        return len(signals), sent


# ── 模块级便捷函数（供 main.py 直接调用） ──────────────────

_default_macd = MACDSignal()


def check_macd_signals(stock_codes=None):
    """便捷函数：检测 MACD 金叉/死叉信号"""
    return _default_macd.check_signals(stock_codes)


def send_macd_alerts(signals):
    """便捷函数：批量发送 MACD 信号"""
    return _default_macd.send_macd_alerts(signals)
