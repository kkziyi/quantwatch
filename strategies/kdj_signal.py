"""
KDJ 金叉/死叉信号检测模块 — 收盘后执行，检测 KDJ(9,3,3) 金叉/死叉并推送飞书

Phase 2b: KDJSignal 类实现，含 KDJ 计算（RSV→K→D→J）、
金叉/死叉判断、超买(>80)/超卖(<20)区域标注、日期级去重推送。
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

logger = logging.getLogger("quantwatch.kdj_signal")


class KDJSignal:
    """
    KDJ(9,3,3) 金叉/死叉信号检测器。

    收盘后执行（非盘中轮询）。
    使用 AKShare stock_zh_a_hist(adjust="qfq") 获取前复权日线数据，
    计算 KDJ(9,3,3)，检测金叉/死叉信号。

    特性：
    - KDJ 计算：RSV→K(EMA3)→D(EMA3)→J
    - 金叉：K[t-1] <= D[t-1] AND K[t] > D[t]
    - 死叉：K[t-1] >= D[t-1] AND K[t] < D[t]
    - 超买区域标注：K >= 80 且 D > 80
    - 超卖区域标注：K <= 20 且 D < 20
    - 去重：key 格式 kdj:{signal_type}:{code}:{date}，按日期区分事件型信号
    """

    def __init__(self, config=None):
        if config is None:
            config = STRATEGIES.get("kdj_signal", {})
        self.enabled = config.get("enabled", False)
        self.n = config.get("n", 9)           # RSV 周期
        self.k_weight = config.get("k", 3)     # K 平滑周期（EMA span）
        self.d_weight = config.get("d", 3)     # D 平滑周期（EMA span）
        self.overbought = config.get("overbought", 80)
        self.oversold = config.get("oversold", 20)
        # KDJ 的 K/D 是 0-100 无量纲百分比，使用绝对阈值过滤 K-D 差值过小的虚假信号
        # 不同于 MACD 的 price×ratio 方式（MACD DIF/DEA 是价格量纲）
        self.diff_gap_min = config.get("diff_gap_min", 1.5)

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
        """获取单只股票近 ~4 个月前复权日线数据

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

        # 取 120 天数据，确保有足够交易日计算 KDJ（N=9 至少需 N+1=10 个交易日）
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
            time.sleep(AKSHARE_DELAY)

        return self._history_cache[code]

    # ── KDJ 计算 ─────────────────────────────────────────────

    def compute_kdj(self, df):
        """计算 KDJ(9,3,3)

        RSV(n) = (close - low_n) / (high_n - low_n) * 100
        K = EMA(RSV, 3)   即 2/3 * K_prev + 1/3 * RSV
        D = EMA(K, 3)     即 2/3 * D_prev + 1/3 * K
        J = 3*K - 2*D

        Args:
            df: DataFrame，需包含 'high', 'low', 'close' 列

        Returns:
            DataFrame，新增 K, D, J 列
        """
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        close = df["close"].astype(float)

        # RSV(n): 用 rolling 计算过去 n 日最低价和最高价
        low_n = low.rolling(window=self.n, min_periods=1).min()
        high_n = high.rolling(window=self.n, min_periods=1).max()

        # RSV = (close - Ln) / (Hn - Ln) * 100，分母为 0 时设为 50
        denom = high_n - low_n
        rsv = pd.Series(50.0, index=close.index)  # 默认 50
        valid = denom > 1e-8
        rsv[valid] = (close[valid] - low_n[valid]) / denom[valid] * 100.0

        # K = EMA(RSV, k_weight), D = EMA(K, d_weight)
        k = rsv.ewm(span=self.k_weight, adjust=False).mean()
        d = k.ewm(span=self.d_weight, adjust=False).mean()
        j = 3 * k - 2 * d

        result = df.copy()
        result["K"] = k
        result["D"] = d
        result["J"] = j

        return result

    # ── 信号检测 ──────────────────────────────────────────────

    def _check_gap_filter(self, k_val, d_val):
        """K-D 差值过滤检测

        KDJ 的 K/D 是 0-100 无量纲百分比，使用绝对阈值过滤：
        |K - D| > diff_gap_min 才算真信号

        Returns:
            True 表示通过过滤（是真信号），False 表示不通过
        """
        gap = abs(k_val - d_val)
        return gap > self.diff_gap_min

    def check_signals(self, stock_codes=None):
        """主检测入口：遍历自选股，检测 KDJ 金叉/死叉信号

        Args:
            stock_codes: 股票代码列表，默认使用 config.STOCKS

        Returns:
            list of dict:
                {
                    "code": "600176",
                    "name": "中国巨石",
                    "type": "golden_cross" / "dead_cross",
                    "label": "金叉" / "死叉" / "超买区金叉" / "超买区死叉" / ...
                    "K": 32.5,
                    "D": 28.1,
                    "J": 41.3,
                    "gap": 4.4,
                    "close": 15.23,
                    "date": "2026-05-29",
                }
        """
        if not self.enabled:
            logger.info("KDJ 信号检测未启用，跳过")
            return []

        if stock_codes is None:
            stock_codes = list(STOCKS.keys())

        signals = []
        logger.info(f"开始 KDJ 信号检测，共 {len(stock_codes)} 只股票...")

        for code in stock_codes:
            try:
                signal = self._check_single(code)
                if signal:
                    signals.append(signal)
            except Exception as e:
                logger.warning(f"  {code}: KDJ 检测失败: {e}")

            time.sleep(AKSHARE_DELAY)

        logger.info(f"KDJ 信号检测完成: 共 {len(signals)} 个信号")
        return signals

    def _check_single(self, code):
        """检测单只股票的 KDJ 金叉/死叉

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

        # 检查数据量：至少需要 N+1 个交易日
        min_days = self.n + 1  # 9+1=10
        if len(df) < min_days:
            logger.info(
                f"  {code}({name}): 数据不足 {min_days} 个交易日（仅 {len(df)} 天），跳过"
            )
            return None

        # 计算 KDJ
        df = self.compute_kdj(df)

        if len(df) < 3:
            return None

        # 最近两天（T-1 = 昨天, T = 今天/最近一天）
        t_row = df.iloc[-1]
        t1_row = df.iloc[-2]

        k_t = float(t_row["K"])
        d_t = float(t_row["D"])
        j_t = float(t_row["J"])
        k_t1 = float(t1_row["K"])
        d_t1 = float(t1_row["D"])
        close_t = float(t_row["close"])
        date_t = str(t_row.get("date", ""))[:10] if "date" in df.columns else ""

        # NaN 检查
        if pd.isna(k_t) or pd.isna(d_t) or pd.isna(k_t1) or pd.isna(d_t1):
            logger.info(f"  {code}({name}): KDJ 数据含 NaN，跳过")
            return None

        # ── 区域判断 ──
        zone = ""
        if k_t >= self.overbought and d_t > self.overbought:
            zone = "超买区"
        elif k_t <= self.oversold and d_t < self.oversold:
            zone = "超卖区"

        # ── 金叉检测 ──
        # 条件：K[t-1] <= D[t-1] AND K[t] > D[t]
        if k_t1 <= d_t1 and k_t > d_t:
            if not self._check_gap_filter(k_t, d_t):
                logger.debug(
                    f"  {code}({name}): 疑似金叉但 K-D 差值过小 "
                    f"(K={k_t:.2f}, D={d_t:.2f}, close={close_t:.2f})，跳过"
                )
                return None

            if zone == "超卖区":
                label = "超卖区金叉"
                emoji = "🟡"
            elif zone == "超买区":
                label = "超买区金叉"
                emoji = "🟠"
            else:
                label = "金叉"
                emoji = "🟢"

            return {
                "code": code,
                "name": name,
                "type": "golden_cross",
                "label": label,
                "emoji": emoji,
                "zone": zone,
                "K": round(k_t, 2),
                "D": round(d_t, 2),
                "J": round(j_t, 2),
                "gap": round(k_t - d_t, 2),
                "close": round(close_t, 2),
                "date": date_t,
            }

        # ── 死叉检测 ──
        # 条件：K[t-1] >= D[t-1] AND K[t] < D[t]
        if k_t1 >= d_t1 and k_t < d_t:
            if not self._check_gap_filter(k_t, d_t):
                logger.debug(
                    f"  {code}({name}): 疑似死叉但 K-D 差值过小 "
                    f"(K={k_t:.2f}, D={d_t:.2f}, close={close_t:.2f})，跳过"
                )
                return None

            if zone == "超买区":
                label = "超买区死叉"
                emoji = "🔴"
            elif zone == "超卖区":
                label = "超卖区死叉"
                emoji = "🔵"
            else:
                label = "死叉"
                emoji = "🔴"

            return {
                "code": code,
                "name": name,
                "type": "dead_cross",
                "label": label,
                "emoji": emoji,
                "zone": zone,
                "K": round(k_t, 2),
                "D": round(d_t, 2),
                "J": round(j_t, 2),
                "gap": round(k_t - d_t, 2),
                "close": round(close_t, 2),
                "date": date_t,
            }

        return None

    # ── 去重 ──────────────────────────────────────────────────

    def _should_send_kdj(self, code, signal_type, date_str):
        """去重检查：同股票同信号类型同日期内不重复推送

        Key 格式:
            kdj:golden_cross:{code}:{date_str}
            kdj:dead_cross:{code}:{date_str}

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

        key = f"kdj:{signal_type}:{code}:{date_str}"
        return key not in state

    def _mark_sent_kdj(self, code, signal_type, date_str):
        """标记 KDJ 信号已发送"""
        state_file = self._state_file_path()
        state = {}

        if os.path.exists(state_file):
            try:
                with open(state_file, "r", encoding="utf-8") as f:
                    state = json.load(f)
            except (json.JSONDecodeError, IOError):
                pass

        key = f"kdj:{signal_type}:{code}:{date_str}"
        state[key] = datetime.now().isoformat()

        os.makedirs(os.path.dirname(state_file), exist_ok=True)
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)

    # ── 飞书推送 ──────────────────────────────────────────────

    def send_kdj_alert(self, signal):
        """发送单条 KDJ 信号飞书卡片通知

        Args:
            signal: check_signals() 返回的 dict

        Returns:
            True 表示发送成功
        """
        if not FEISHU_WEBHOOK_URL:
            logger.warning("飞书 Webhook URL 未配置，跳过 KDJ 推送")
            return False

        code = signal["code"]
        name = signal["name"]
        signal_type = signal["type"]
        label = signal["label"]
        emoji = signal["emoji"]
        k_val = signal["K"]
        d_val = signal["D"]
        j_val = signal["J"]
        close_val = signal["close"]

        # 去重检查
        if not self._should_send_kdj(code, signal_type, signal.get("date", "")):
            logger.debug(f"{name}({code}) KDJ {label} 已在去重窗口内，跳过")
            return False

        # 构建区域标签
        zone_tag = ""
        if signal.get("zone"):
            zone_tag = f"（{signal['zone']}）"

        # 构建卡片
        card = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": f"{emoji} KDJ {label}{zone_tag}",
                    },
                    "template": "red" if signal_type == "dead_cross" else "green",
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": (
                                f"**{name}**（{code}）\\n"
                                f"K=**{k_val:.2f}**　D=**{d_val:.2f}**　"
                                f"J=**{j_val:.2f}**　收盘={close_val:.2f}"
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
                self._mark_sent_kdj(code, signal_type, signal.get("date", ""))
                logger.info(
                    f"✅ KDJ 飞书推送成功: {name}({code}) {label} "
                    f"K={k_val:.2f} D={d_val:.2f} J={j_val:.2f}"
                )
                return True
            else:
                logger.error(f"KDJ 飞书推送失败: HTTP {resp.status_code} {body}")
                return False
        except requests.RequestException as e:
            logger.error(f"KDJ 飞书推送异常: {e}")
            return False

    def send_kdj_alerts(self, signals):
        """批量发送 KDJ 信号

        Args:
            signals: check_signals() 返回的列表

        Returns:
            实际发送条数
        """
        if not signals or not FEISHU_WEBHOOK_URL:
            return 0

        sent_count = 0
        for signal in signals:
            if self.send_kdj_alert(signal):
                sent_count += 1
                time.sleep(0.5)  # 飞书 Webhook 限速

        return sent_count

    # ── 主入口（收盘后调用） ──────────────────────────────────

    def run(self, stock_codes=None):
        """收盘后主入口：检测 KDJ 信号并推送

        Args:
            stock_codes: 股票代码列表，默认使用 config.STOCKS

        Returns:
            (检测信号数, 推送成功数)
        """
        signals = self.check_signals(stock_codes)

        # 日志输出
        for sig in signals:
            zone_tag = f"（{sig['zone']}）" if sig.get("zone") else ""
            logger.info(
                f"  {sig['emoji']} KDJ {sig['label']}{zone_tag}: "
                f"{sig['name']}({sig['code']}) "
                f"K={sig['K']:.2f} D={sig['D']:.2f} J={sig['J']:.2f}"
            )

        sent = self.send_kdj_alerts(signals)
        return len(signals), sent


# ── 模块级便捷函数（供 main.py / AfterCloseScheduler 直接调用） ──

_default_kdj = KDJSignal()


def check_kdj_signals(stock_codes=None):
    """便捷函数：检测 KDJ 金叉/死叉信号"""
    return _default_kdj.check_signals(stock_codes)


def send_kdj_alerts(signals):
    """便捷函数：批量发送 KDJ 信号"""
    return _default_kdj.send_kdj_alerts(signals)
