"""
QuantWatch 回测报告模块 — 生成含 matplotlib 图表的 HTML 报告

Phase 2c B-3: generate_report() 输入 BacktestEngine.run() + calculate_metrics()
的输出，生成 4 张图表 + metrics 表格的完整 HTML 报告。

用法:
    from backtesting.engine import BacktestEngine
    from backtesting.metrics import calculate_metrics
    from backtesting.reporter import generate_report

    engine = BacktestEngine("macd", ["600176"], "2025-01-01", "2025-12-31")
    result = engine.run()
    result["metrics"] = calculate_metrics(result)  # 可选，reporter 会自动计算
    path = generate_report(result)
    print(f"报告已生成: {path}")
"""

import base64
import io
import logging
import os
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # 无 GUI 后端，线程安全
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from matplotlib.lines import Line2D
import numpy as np
import pandas as pd

from backtesting.metrics import calculate_metrics, _calc_trade_pnls

logger = logging.getLogger("quantwatch.reporter")

# ── 项目路径 ─────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.parent
REPORTS_DIR = PROJECT_ROOT / "data" / "reports"

# 中文字体支持
plt.rcParams["font.sans-serif"] = ["SimHei", "WenQuanYi Micro Hei",
                                    "DejaVu Sans", "Arial"]
plt.rcParams["axes.unicode_minus"] = False


# ═══════════════════════════════════════════════════════════════
# 公开接口
# ═══════════════════════════════════════════════════════════════

def generate_report(result: dict) -> str:
    """生成回测 HTML 报告

    Args:
        result: BacktestEngine.run() 输出，格式:
            {"trades": [...], "equity_curve": [...], "config": {...}, "metrics": ...}

    Returns:
        HTML 文件路径 (data/reports/backtest_<策略>_<时间戳>.html)
    """
    # ── 确保 metrics 存在 ──
    metrics = result.get("metrics")
    if metrics is None:
        metrics = calculate_metrics(result)

    trades = result.get("trades", [])
    equity_curve = result.get("equity_curve", [])
    config = result.get("config", {})
    initial_cash = float(config.get("initial_cash", 1_000_000))

    # ── 确保报告目录存在 ──
    os.makedirs(REPORTS_DIR, exist_ok=True)

    # ── 无交易 → 简约版报告 ──
    if not trades:
        html = _build_simple_report(config, metrics)
        return _save_html(html, config)

    # ── 计算交易 P&L ──
    trade_pnls = _calc_trade_pnls(trades, initial_cash)

    # ── 生成 4 张图表 ──
    chart_equity = _chart_equity_curve(equity_curve, trades, initial_cash)
    chart_drawdown = _chart_drawdown(equity_curve)
    chart_monthly = _chart_monthly_returns(equity_curve)
    chart_trade_dist = _chart_trade_distribution(trade_pnls)

    # ── 组装 HTML ──
    html = _build_full_report(
        config=config,
        metrics=metrics,
        chart_equity=chart_equity,
        chart_drawdown=chart_drawdown,
        chart_monthly=chart_monthly,
        chart_trade_dist=chart_trade_dist,
    )

    return _save_html(html, config)


# ═══════════════════════════════════════════════════════════════
# 图表生成（返回 base64 字符串）
# ═══════════════════════════════════════════════════════════════

def _fig_to_base64(fig: plt.Figure) -> str:
    """将 matplotlib Figure 转为 base64 字符串"""
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode("utf-8")


def _chart_equity_curve(
    equity_curve: list[dict],
    trades: list[dict],
    initial_cash: float,
) -> str:
    """权益曲线 + 买卖标记"""
    fig, ax = plt.subplots(figsize=(12, 5))
    fig.patch.set_facecolor("#1a1a2e")
    ax.set_facecolor("#1a1a2e")

    dates = [e["date"] for e in equity_curve]
    values = [e["total_value"] for e in equity_curve]

    # 权益曲线
    ax.plot(dates, values, color="#00d4aa", linewidth=1.8, label="Portfolio Value")
    ax.axhline(y=initial_cash, color="#666", linestyle="--", linewidth=0.8,
               label=f"Initial ({initial_cash:,.0f})")

    # 买卖标记 — 将交易日期映射到最近的权益曲线日期
    eq_dates = {e["date"]: e["total_value"] for e in equity_curve}
    for t in trades:
        if t["date"] in eq_dates:
            y = eq_dates[t["date"]]
            if t["action"] == "buy":
                ax.scatter(t["date"], y, color="#00ff88", marker="^",
                           s=60, zorder=5, edgecolors="none")
            else:
                ax.scatter(t["date"], y, color="#ff4757", marker="v",
                           s=60, zorder=5, edgecolors="none")

    # 图例手动添加标记
    custom_lines = [
        Line2D([0], [0], color="#00d4aa", linewidth=2),
        Line2D([0], [0], color="#666", linestyle="--", linewidth=1),
        Line2D([0], [0], marker="^", color="w", markerfacecolor="#00ff88",
               markersize=8, linestyle="None"),
        Line2D([0], [0], marker="v", color="w", markerfacecolor="#ff4757",
               markersize=8, linestyle="None"),
    ]
    ax.legend(custom_lines, ["Equity", "Initial", "Buy", "Sell"],
              loc="upper left", framealpha=0.3, facecolor="#333",
              labelcolor="white", fontsize=9)

    # 样式
    ax.set_title("Equity Curve", color="white", fontsize=14, pad=12)
    ax.set_ylabel("Portfolio Value (¥)", color="#aaa")
    ax.tick_params(colors="#aaa", labelsize=8)
    ax.yaxis.set_major_formatter(
        mticker.FuncFormatter(lambda x, _: f"{x/1e4:.0f}万"))
    ax.grid(True, alpha=0.15, color="white")

    # X 轴日期标签稀疏化
    n_dates = len(dates)
    if n_dates > 30:
        step = max(1, n_dates // 15)
        ax.set_xticks(range(0, n_dates, step))
        ax.set_xticklabels([dates[i] for i in range(0, n_dates, step)],
                           rotation=45, ha="right", fontsize=7)

    fig.tight_layout()
    return _fig_to_base64(fig)


def _chart_drawdown(equity_curve: list[dict]) -> str:
    """回撤面积图"""
    fig, ax = plt.subplots(figsize=(12, 4))
    fig.patch.set_facecolor("#1a1a2e")
    ax.set_facecolor("#1a1a2e")

    if len(equity_curve) < 2:
        ax.text(0.5, 0.5, "Insufficient data", ha="center", va="center",
                color="#888", transform=ax.transAxes, fontsize=14)
        fig.tight_layout()
        return _fig_to_base64(fig)

    dates = [e["date"] for e in equity_curve]
    values = [e["total_value"] for e in equity_curve]

    # 计算回撤序列
    peak = values[0]
    drawdowns = []
    for v in values:
        if v > peak:
            peak = v
        dd = (v - peak) / peak * 100  # 百分比
        drawdowns.append(dd)

    # 面积图
    ax.fill_between(range(len(dates)), drawdowns, 0,
                    color="#ff4757", alpha=0.4)
    ax.plot(range(len(dates)), drawdowns, color="#ff6b81", linewidth=1.2)

    # 当前最大回撤标记
    min_idx = drawdowns.index(min(drawdowns))
    ax.annotate(f"{drawdowns[min_idx]:.1f}%",
                xy=(min_idx, drawdowns[min_idx]),
                xytext=(min_idx + 5, drawdowns[min_idx] - 3),
                color="#ff4757", fontsize=10, fontweight="bold",
                arrowprops=dict(arrowstyle="->", color="#ff4757", lw=1.2))

    # 样式
    ax.set_title("Drawdown", color="white", fontsize=14, pad=12)
    ax.set_ylabel("Drawdown %", color="#aaa")
    ax.tick_params(colors="#aaa", labelsize=8)
    ax.yaxis.set_major_formatter(
        mticker.FuncFormatter(lambda x, _: f"{x:.0f}%"))
    ax.grid(True, alpha=0.15, color="white")

    # X 轴日期标签
    n_dates = len(dates)
    if n_dates > 30:
        step = max(1, n_dates // 15)
        ax.set_xticks(range(0, n_dates, step))
        ax.set_xticklabels([dates[i] for i in range(0, n_dates, step)],
                           rotation=45, ha="right", fontsize=7)

    fig.tight_layout()
    return _fig_to_base64(fig)


def _chart_monthly_returns(equity_curve: list[dict]) -> str:
    """月度收益率热力图 (12 个月 × N 年)"""
    fig, ax = plt.subplots(figsize=(10, 6))
    fig.patch.set_facecolor("#1a1a2e")
    ax.set_facecolor("#1a1a2e")

    if len(equity_curve) < 2:
        ax.text(0.5, 0.5, "Insufficient data for monthly returns",
                ha="center", va="center", color="#888",
                transform=ax.transAxes, fontsize=12)
        fig.tight_layout()
        return _fig_to_base64(fig)

    # 构建月度收益表
    df = pd.DataFrame(equity_curve)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").set_index("date")

    # 月度重采样
    monthly = df["total_value"].resample("ME").last()
    if len(monthly) < 2:
        ax.text(0.5, 0.5, "Need at least 2 months of data",
                ha="center", va="center", color="#888",
                transform=ax.transAxes, fontsize=12)
        fig.tight_layout()
        return _fig_to_base64(fig)

    monthly_returns = monthly.pct_change().dropna()

    # 构建 12×N 矩阵
    monthly_returns.index = monthly_returns.index.to_period("M")
    years = sorted(set(m.year for m in monthly_returns.index))
    months = list(range(1, 13))

    data_matrix = np.full((12, len(years)), np.nan)
    for i, year in enumerate(years):
        for j, month in enumerate(months):
            key = pd.Period(f"{year}-{month:02d}", freq="M")
            if key in monthly_returns.index:
                data_matrix[j, i] = monthly_returns.loc[key] * 100

    # 热力图
    cmap = plt.cm.RdYlGn
    im = ax.imshow(data_matrix, aspect="auto", cmap=cmap,
                   vmin=-15, vmax=15, interpolation="nearest")

    # 标注
    month_labels = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    ax.set_yticks(range(12))
    ax.set_yticklabels(month_labels, color="#aaa", fontsize=9)
    ax.set_xticks(range(len(years)))
    ax.set_xticklabels(years, color="#aaa", fontsize=9)

    # 在每个格子中标注数值
    for i in range(12):
        for j in range(len(years)):
            val = data_matrix[i, j]
            if not np.isnan(val):
                text_color = "white" if abs(val) > 8 else "black"
                ax.text(j, i, f"{val:.1f}%", ha="center", va="center",
                        fontsize=7, color=text_color, fontweight="bold")

    ax.set_title("Monthly Returns (%)", color="white", fontsize=14, pad=12)

    # Colorbar
    cbar = fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    cbar.ax.tick_params(colors="#aaa", labelsize=8)
    cbar.set_label("Return %", color="#aaa")

    fig.tight_layout()
    return _fig_to_base64(fig)


def _chart_trade_distribution(trade_pnls: list[dict]) -> str:
    """交易盈亏分布直方图"""
    fig, ax = plt.subplots(figsize=(10, 5))
    fig.patch.set_facecolor("#1a1a2e")
    ax.set_facecolor("#1a1a2e")

    if not trade_pnls:
        ax.text(0.5, 0.5, "No completed trades",
                ha="center", va="center", color="#888",
                transform=ax.transAxes, fontsize=14)
        fig.tight_layout()
        return _fig_to_base64(fig)

    pnl_pcts = [p["pnl_pct"] * 100 for p in trade_pnls]  # 转为 %

    n_bins = min(20, max(5, int(len(pnl_pcts) ** 0.5) * 2))
    ax.hist(pnl_pcts, bins=n_bins, color="#00d4aa", alpha=0.7,
            edgecolor="#2a2a4e", linewidth=2)

    # 均值线
    mean_val = np.mean(pnl_pcts)
    ax.axvline(x=mean_val, color="white", linestyle="--", linewidth=1.2,
               label=f"Mean: {mean_val:+.2f}%")

    # 零线
    ax.axvline(x=0, color="#666", linestyle="-", linewidth=0.8)

    # 样式
    ax.set_title("Trade P&L Distribution", color="white", fontsize=14, pad=12)
    ax.set_xlabel("P&L per Trade (%)", color="#aaa")
    ax.set_ylabel("Frequency", color="#aaa")
    ax.tick_params(colors="#aaa", labelsize=9)
    ax.legend(loc="upper right", facecolor="#333", labelcolor="white",
              framealpha=0.4, fontsize=9)
    ax.grid(True, alpha=0.15, color="white")

    fig.tight_layout()
    return _fig_to_base64(fig)


# ═══════════════════════════════════════════════════════════════
# HTML 构建
# ═══════════════════════════════════════════════════════════════

def _build_full_report(
    config: dict,
    metrics: dict,
    chart_equity: str,
    chart_drawdown: str,
    chart_monthly: str,
    chart_trade_dist: str,
) -> str:
    """构建完整 HTML 报告"""
    strategy = config.get("strategy", "unknown")
    start = config.get("start", "")
    end = config.get("end", "")
    codes = ", ".join(config.get("codes", []))
    initial_cash = config.get("initial_cash", 1_000_000)
    pos_size = config.get("position_size", 1000)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Backtest Report — {strategy.upper()}</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto,
                 'Noto Sans SC', sans-serif;
    background: #0f0f1a;
    color: #ccc;
    line-height: 1.6;
    padding: 20px;
  }}
  .container {{ max-width: 1200px; margin: 0 auto; }}
  h1 {{ color: #00d4aa; font-size: 1.8em; margin-bottom: 4px; }}
  .subtitle {{ color: #888; font-size: 0.9em; margin-bottom: 24px; }}
  .section {{
    background: #1a1a2e;
    border-radius: 8px;
    padding: 20px;
    margin-bottom: 20px;
  }}
  .section h2 {{
    color: #00d4aa;
    font-size: 1.2em;
    margin-bottom: 16px;
    border-bottom: 1px solid #2a2a4e;
    padding-bottom: 8px;
  }}
  img.chart {{
    width: 100%;
    max-width: 100%;
    border-radius: 4px;
    display: block;
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 0.95em;
  }}
  th, td {{
    padding: 10px 14px;
    text-align: left;
    border-bottom: 1px solid #2a2a4e;
  }}
  th {{ color: #00d4aa; font-weight: 600; }}
  tr:hover {{ background: rgba(0,212,170,0.05); }}
  .metric-val {{ font-weight: 700; }}
  .positive {{ color: #00d4aa; }}
  .negative {{ color: #ff4757; }}
  .info-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 12px;
    margin-bottom: 16px;
  }}
  .info-card {{
    background: rgba(255,255,255,0.03);
    border-radius: 6px;
    padding: 10px 14px;
  }}
  .info-card .label {{ color: #888; font-size: 0.8em; }}
  .info-card .value {{ color: #ddd; font-size: 1.1em; font-weight: 600; }}
  .warning {{ color: #ffa502; font-style: italic; margin-top: 8px; }}
  .footer {{
    text-align: center;
    color: #555;
    font-size: 0.8em;
    margin-top: 24px;
    padding: 12px;
  }}

  /* 移动端适配 */
  @media (max-width: 768px) {{
    body {{ padding: 10px; }}
    h1 {{ font-size: 1.4em; }}
    .section {{ padding: 14px; }}
    th, td {{ padding: 6px 8px; font-size: 0.85em; }}
    .info-grid {{ grid-template-columns: repeat(2, 1fr); }}
  }}
</style>
</head>
<body>
<div class="container">

  <h1>📊 Backtest Report — {strategy.upper()}</h1>
  <div class="subtitle">
    {codes} | {start} ~ {end} | Position: {pos_size} shares |
    Initial: ¥{initial_cash:,.0f}
  </div>

  <!-- 配置信息 -->
  <div class="info-grid">
    <div class="info-card">
      <div class="label">Strategy</div>
      <div class="value">{strategy.upper()}</div>
    </div>
    <div class="info-card">
      <div class="label">Period</div>
      <div class="value">{start} → {end}</div>
    </div>
    <div class="info-card">
      <div class="label">Stocks</div>
      <div class="value">{len(config.get("codes", []))} codes</div>
    </div>
    <div class="info-card">
      <div class="label">Initial Cash</div>
      <div class="value">¥{initial_cash:,.0f}</div>
    </div>
  </div>

  {_build_metrics_section(metrics)}

  <!-- 图表 -->
  <div class="section">
    <h2>📈 Equity Curve</h2>
    <img class="chart" src="data:image/png;base64,{chart_equity}"
         alt="Equity Curve">
  </div>

  <div class="section">
    <h2>📉 Drawdown</h2>
    <img class="chart" src="data:image/png;base64,{chart_drawdown}"
         alt="Drawdown">
  </div>

  <div class="section">
    <h2>📅 Monthly Returns</h2>
    <img class="chart" src="data:image/png;base64,{chart_monthly}"
         alt="Monthly Returns Heatmap">
  </div>

  <div class="section">
    <h2>📊 Trade P&amp;L Distribution</h2>
    <img class="chart" src="data:image/png;base64,{chart_trade_dist}"
         alt="Trade Distribution">
  </div>

  <div class="footer">
    Generated by QuantWatch Backtesting Engine |
    {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
  </div>

</div>
</body>
</html>"""


def _build_simple_report(config: dict, metrics: dict) -> str:
    """无交易时的简约版报告"""
    strategy = config.get("strategy", "unknown")
    start = config.get("start", "")
    end = config.get("end", "")
    codes = ", ".join(config.get("codes", []))
    warning = metrics.get("warning", "No trading signals generated.")

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Backtest Report — {strategy.upper()} (No Trades)</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto,
                 'Noto Sans SC', sans-serif;
    background: #0f0f1a;
    color: #ccc;
    padding: 40px 20px;
  }}
  .container {{ max-width: 600px; margin: 0 auto; text-align: center; }}
  h1 {{ color: #ffa502; font-size: 1.6em; margin-bottom: 16px; }}
  .box {{
    background: #1a1a2e;
    border-radius: 8px;
    padding: 40px;
    margin-top: 20px;
  }}
  .box p {{ color: #aaa; line-height: 1.8; }}
  .info {{ color: #888; font-size: 0.9em; margin-top: 20px; }}
  .footer {{
    text-align: center;
    color: #555;
    font-size: 0.8em;
    margin-top: 40px;
  }}
</style>
</head>
<body>
<div class="container">
  <h1>⚠️ No Trades</h1>
  <div class="box">
    <p>Strategy <strong style="color:#00d4aa">{strategy.upper()}</strong>
       did not generate any trading signals.</p>
    <p>{warning}</p>
    <div class="info">
      Stocks: {codes}<br>
      Period: {start} ~ {end}<br>
      Initial Cash: ¥{config.get('initial_cash', 0):,.0f}
    </div>
  </div>
  <div class="footer">
    Generated by QuantWatch Backtesting Engine |
    {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
  </div>
</div>
</body>
</html>"""


def _build_metrics_section(metrics: dict) -> str:
    """构建 metrics 表格 HTML"""
    if not metrics or metrics.get("total_trades", 0) == 0:
        warning = metrics.get("warning", "")
        return f"""
  <div class="section">
    <h2>📋 Performance Metrics</h2>
    <p class="warning">{warning}</p>
  </div>"""

    def _cls(val, is_pct=False):
        """根据值返回 positive/negative CSS class"""
        if val is None:
            return ""
        if isinstance(val, (int, float)):
            if val > 0:
                return "positive"
            elif val < 0:
                return "negative"
        return ""

    def _fmt(val, is_pct=False):
        """格式化数值"""
        if val is None:
            return "∞"
        if is_pct:
            return f"{val*100:+.2f}%"
        if isinstance(val, float):
            return f"{val:+.4f}" if abs(val) < 10 else f"{val:,.2f}"
        return str(val)

    rows = [
        ("Total Return", metrics.get("total_return", 0), True),
        ("Annual Return", metrics.get("annual_return", 0), True),
        ("Max Drawdown", metrics.get("max_drawdown", 0), True),
        ("Sharpe Ratio", metrics.get("sharpe_ratio", 0), False),
        ("Win Rate", metrics.get("win_rate", 0), True),
        ("Profit Factor", metrics.get("profit_factor", 0), False),
        ("Total Trades", metrics.get("total_trades", 0), False),
        ("Avg Profit/Trade", metrics.get("avg_profit_per_trade", 0), True),
        ("Max Profit Trade", metrics.get("max_profit_trade", 0), True),
        ("Max Loss Trade", metrics.get("max_loss_trade", 0), True),
    ]

    table_rows = ""
    for name, val, is_pct in rows:
        table_rows += (
            f"    <tr>"
            f"<td>{name}</td>"
            f"<td class=\"metric-val {_cls(val, is_pct)}\">{_fmt(val, is_pct)}</td>"
            f"</tr>\n"
        )

    return f"""
  <div class="section">
    <h2>📋 Performance Metrics</h2>
    <table>
      <thead><tr><th>Metric</th><th>Value</th></tr></thead>
      <tbody>
{table_rows}      </tbody>
    </table>
  </div>"""


# ═══════════════════════════════════════════════════════════════
# 文件保存
# ═══════════════════════════════════════════════════════════════

def _save_html(html: str, config: dict) -> str:
    """保存 HTML 到 data/reports/ 目录"""
    strategy = config.get("strategy", "unknown")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"backtest_{strategy}_{timestamp}.html"
    filepath = REPORTS_DIR / filename

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html)

    logger.info(f"报告已保存: {filepath}")
    return str(filepath)
