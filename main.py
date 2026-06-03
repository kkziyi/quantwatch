#!/usr/bin/env python3
"""
QuantWatch — A 股量化监控系统 主入口
盘中自动轮询自选股行情，异动实时推送飞书
"""
import logging
import os
import signal
import sys
import time
from datetime import datetime, time as dt_time

# 确保项目根目录在 sys.path 中
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

from config import (
    STOCKS,
    CHECK_INTERVAL,
    AKSHARE_DELAY,
    TRADING_START,
    TRADING_END,
    LOG_FILE,
    LOG_FORMAT,
    LOG_DATE_FORMAT,
    FEISHU_WEBHOOK_URL,
)

from strategies.price_alert import PriceAlert, check_alerts, get_quote_summary
from strategies.volume_alert import (
    load_daily_baselines, check_volume_alerts, send_volume_alerts,
)
from strategies.turnover_alert import (
    load_turnover_baselines, check_turnover_alerts, send_turnover_alerts,
    get_current_turnover,
)
from strategies.macd_signal import (
    check_macd_signals, send_macd_alerts,
)
from strategies.kdj_signal import check_kdj_signals, send_kdj_alerts
from strategies.rsi_signal import check_rsi_signals, send_rsi_alerts
from notifiers.feishu import send_summary
from portfolio import PortfolioManager
from reports.daily_brief import run_daily_brief, get_push_scheduler
from portfolio.daily_report import run_portfolio_daily
from reports.intraday_scanner import run_intraday_scan

# ── 成交量基准全局缓存 ──────────────────────────────────────
_daily_baselines = None        # dict: {code: baseline_volume, ...}
_baselines_loaded_today = None  # datetime.date: 基准已加载的日期

# ── 换手率基准全局缓存 ──────────────────────────────────────
_turnover_baselines = None        # dict: {code: {...}, ...}
_turnover_loaded_today = None     # datetime.date: 换手率基准已加载的日期

# ── 收盘后调度框架 ──────────────────────────────────────────
from schedulers import AfterCloseScheduler

_after_close = AfterCloseScheduler()

# ── 推送调度器（开盘/盘中/收盘统一编排）───────────────────────
_push_scheduler = get_push_scheduler()


def _run_macd_after_close():
    """MACD 金叉/死叉检测 — 收盘后执行"""
    signals = check_macd_signals()
    if signals:
        return send_macd_alerts(signals)
    return 0


def _run_kdj_after_close():
    """KDJ 超买超卖信号检测 — 收盘后执行"""
    signals = check_kdj_signals()
    if signals:
        return send_kdj_alerts(signals)
    return 0


def _run_rsi_after_close():
    """RSI 超买超卖信号检测 — 收盘后执行"""
    signals = check_rsi_signals()
    if signals:
        return send_rsi_alerts(signals)
    return 0


_after_close.register("macd", _run_macd_after_close, order=10)
_after_close.register("kdj", _run_kdj_after_close, order=20)
_after_close.register("rsi", _run_rsi_after_close, order=30)
_after_close.register("daily_brief", run_daily_brief, order=50)
_after_close.register("portfolio_daily", run_portfolio_daily, order=60)

# ── 日志配置 ────────────────────────────────────────────────
os.makedirs(os.path.dirname(os.path.join(PROJECT_ROOT, LOG_FILE)), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    datefmt=LOG_DATE_FORMAT,
    handlers=[
        logging.FileHandler(os.path.join(PROJECT_ROOT, LOG_FILE), encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("quantwatch")

# ── 优雅退出 ────────────────────────────────────────────────
_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    logger.info(f"收到信号 {signum}，准备退出...")
    _shutdown = True


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


# ── 交易时间判断 ────────────────────────────────────────────
def is_trading_time() -> bool:
    """判断当前是否在 A 股交易时段（含午休排除）"""
    now = datetime.now()
    if not PriceAlert.is_trading_day(now):
        return False
    return PriceAlert.is_trading_time(now)


def minutes_to_market() -> tuple:
    """
    返回距离下次开市的时间和状态

    Returns:
        (minutes, status) 其中 status 为: "trading", "before_open", "after_close", "weekend"
    """
    now = datetime.now()
    t = now.time()
    start = dt_time.fromisoformat(TRADING_START)
    end = dt_time.fromisoformat(TRADING_END)

    if now.weekday() >= 5:
        # 周末 → 下周一 9:30
        days_until_monday = 7 - now.weekday()
        next_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
        next_open += datetime.timedelta(days=days_until_monday)
        return int((next_open - now).total_seconds() / 60), "weekend"

    if t < start:
        # 盘前 → 今天 9:30
        next_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
        return int((next_open - now).total_seconds() / 60), "before_open"

    if start <= t <= end:
        return 0, "trading"

    # 盘后 → 明天 9:30
    if now.weekday() == 4:
        # 周五盘后 → 下周一
        next_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
        next_open += datetime.timedelta(days=3)
    else:
        next_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
        next_open += datetime.timedelta(days=1)

    return int((next_open - now).total_seconds() / 60), "after_close"


# ── 主循环 ──────────────────────────────────────────────────
def run_once() -> int:
    """
    执行一次轮询检查

    Returns:
        触发的异动数量
    """
    logger.info(f"── 轮询开始 ── 自选股: {len(STOCKS)} 只 ──")
    try:
        alerts, watchlist = check_alerts()
    except Exception as e:
        logger.error(f"行情获取失败: {e}")
        return 0

    # 输出行情摘要
    summary = get_quote_summary(watchlist)
    logger.info(f"自选股行情:\n{summary}")

    # 发送飞书通知
    if alerts:
        logger.info(f"共 {len(alerts)} 条异动触发")
        for alert in alerts:
            code, name, price, pct, direction = alert[0], alert[1], alert[2], alert[3], alert[4]
            is_limit = alert[5] if len(alert) >= 6 else False
            sign = "+" if direction == "up" else "-"
            limit_tag = " [涨跌停触板]" if is_limit else ""
            logger.info(
                f"  → {name}({code}) {sign}{abs(pct)*100:.2f}% 价格={price:.2f}{limit_tag}"
            )
        send_summary(alerts)
    else:
        logger.info("无触发，一切平稳")

    # ── 成交量异动检测 ──
    global _daily_baselines
    if _daily_baselines:
        try:
            vol_alerts = check_volume_alerts(watchlist, _daily_baselines)
            if vol_alerts:
                sent = send_volume_alerts(vol_alerts)
                logger.info(f"成交量异动: {len(vol_alerts)} 条, 已推送 {sent} 条")
        except Exception as e:
            logger.warning(f"成交量检测失败: {e}")

    # ── 换手率异动检测 ──
    global _turnover_baselines
    if _turnover_baselines:
        try:
            current_to = get_current_turnover(list(STOCKS.keys()))
            if current_to:
                to_alerts = check_turnover_alerts(current_to, _turnover_baselines)
                if to_alerts:
                    sent = send_turnover_alerts(to_alerts)
                    logger.info(f"换手率异动: {len(to_alerts)} 条, 已推送 {sent} 条")
        except Exception as e:
            logger.warning(f"换手率检测失败: {e}")

    logger.info(f"── 轮询结束 ── 下次检查: {CHECK_INTERVAL}s 后 ──")
    return len(alerts)


def run_loop():
    """主循环：盘中轮询，非交易时间休眠"""
    logger.info("=" * 50)
    logger.info("QuantWatch 启动")
    logger.info(f"自选股数量: {len(STOCKS)}")
    logger.info(f"轮询间隔:   {CHECK_INTERVAL}s")
    logger.info(f"交易时段:   {TRADING_START} - {TRADING_END}")
    logger.info(f"飞书推送:   {'已配置' if FEISHU_WEBHOOK_URL else '未配置'}")
    logger.info("=" * 50)

    check_count = 0
    alert_count = 0

    while not _shutdown:
        global _daily_baselines, _baselines_loaded_today
        global _turnover_baselines, _turnover_loaded_today
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        trading = is_trading_time()

        if trading:
            # ── 首次进入交易时段：加载成交量基准 ──
            today = datetime.now().date()
            if _baselines_loaded_today != today:
                logger.info("── 加载今日成交量基准 ──")
                try:
                    _daily_baselines = load_daily_baselines(list(STOCKS.keys()))
                    _baselines_loaded_today = today
                    logger.info(
                        f"成交量基准已加载: {len(_daily_baselines)} 只股票"
                    )
                except Exception as e:
                    logger.warning(f"成交量基准加载失败: {e}")
                    _daily_baselines = None

            # ── 首次进入交易时段：加载换手率基准 ──
            if _turnover_loaded_today != today:
                logger.info("── 加载今日换手率基准 ──")
                try:
                    _turnover_baselines = load_turnover_baselines(list(STOCKS.keys()))
                    _turnover_loaded_today = today
                    logger.info(
                        f"换手率基准已加载: {len(_turnover_baselines)} 只股票"
                    )
                except Exception as e:
                    logger.warning(f"换手率基准加载失败: {e}")
                    _turnover_baselines = None

            check_count += 1
            logger.info(f"[第 {check_count} 次检查] {current_time}")
            n = run_once()
            alert_count += n

            # ── 盘中全市场扫描（每 15 分钟）──
            try:
                signals = run_intraday_scan()
                if signals > 0:
                    logger.info(f"盘中扫描: 本轮推送 {signals} 条买入信号")
            except Exception as e:
                logger.warning(f"盘中扫描异常: {e}")

            # ── 推送调度（9:25 开盘简报 + 持仓急报）──
            try:
                push_results = _push_scheduler.check_all()
                if push_results:
                    logger.info(f"推送调度: {push_results}")
            except Exception as e:
                logger.warning(f"推送调度异常: {e}")

            # 等待下次轮询（但支持信号中断）
            for _ in range(CHECK_INTERVAL):
                if _shutdown:
                    break
                time.sleep(1)
        else:
            # ── 离开交易时段：释放基准缓存 ──
            if _baselines_loaded_today is not None:
                _daily_baselines = None
                _baselines_loaded_today = None
                logger.info("成交量基准已释放")
            if _turnover_loaded_today is not None:
                _turnover_baselines = None
                _turnover_loaded_today = None
                logger.info("换手率基准已释放")

            mins, status = minutes_to_market()

            # ── 盘后调度：依次运行所有收盘后任务 ──
            if status == "after_close":
                logger.info("── 收盘后调度开始 ──")
                results = _after_close.run_pending()
                if results:
                    for name, result in results:
                        logger.info(f"  收盘后任务 [{name}] 完成: {result}")
                else:
                    logger.info("  无待执行任务（今日已完成）")

            # ── 推送调度（盘前开盘简报 9:25 等）──
            try:
                push_results = _push_scheduler.check_all()
                if push_results:
                    logger.info(f"推送调度: {push_results}")
            except Exception as e:
                logger.warning(f"推送调度异常: {e}")
            status_text = {
                "before_open": "盘前",
                "after_close": "盘后",
                "weekend": "周末",
            }.get(status, status)

            sleep_seconds = min(mins * 60, 3600)  # 最多休眠 1 小时
            logger.info(
                f"[{status_text}] {current_time} — "
                f"距下次开市约 {mins} 分钟，休眠 {sleep_seconds}s"
            )
            for _ in range(sleep_seconds):
                if _shutdown:
                    break
                time.sleep(1)

    logger.info(f"QuantWatch 已退出。共检查 {check_count} 次，触发 {alert_count} 条异动")



# ── CLI 入口 ──────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="QuantWatch — A 股量化监控")
    parser.add_argument(
        "--once",
        action="store_true",
        help="仅执行一次轮询后退出（用于手动测试）",
    )
    parser.add_argument(
        "--now",
        action="store_true",
        help="强制执行一次轮询（即使非交易时间也运行）",
    )

    # ── 回测参数 ────────────────────────────────────────────
    parser.add_argument(
        "--backtest",
        action="store_true",
        help="执行历史回测（需配合 --strategy / --start / --end）",
    )
    parser.add_argument(
        "--strategy",
        type=str,
        default="macd",
        choices=["macd", "kdj", "rsi"],
        help="回测策略名称（默认: macd）",
    )
    parser.add_argument(
        "--codes",
        nargs="+",
        default=None,
        help="回测股票代码列表（默认: config.STOCKS 全部）",
    )
    parser.add_argument(
        "--start",
        type=str,
        default="2025-01-01",
        help="回测起始日期 YYYY-MM-DD（默认: 2025-01-01）",
    )
    parser.add_argument(
        "--end",
        type=str,
        default=None,
        help="回测结束日期 YYYY-MM-DD（默认: 今天）",
    )
    parser.add_argument(
        "--fq",
        type=str,
        default="qfq",
        choices=["qfq", "hfq", ""],
        help="复权类型（默认: qfq 前复权）",
    )
    parser.add_argument(
        "--cash",
        type=float,
        default=1_000_000,
        help="回测初始资金（默认: 1000000）",
    )

    # ── 组合管理参数 ─────────────────────────────────────────
    parser.add_argument(
        "--portfolio",
        type=str,
        nargs="?",
        const="list",
        choices=["list", "create", "delete", "show", "add", "remove", "update", "import", "pnl"],
        help="组合管理子命令: list/create/delete/show/add/remove/update/import/pnl",
    )
    parser.add_argument("--name", type=str, help="组合名称")
    parser.add_argument("--desc", type=str, default="", help="组合描述（用于 create）")
    parser.add_argument("--code", type=str, help="股票代码")
    parser.add_argument("--name2", type=str, help="股票名称（--name 已被组合名占用时使用）")
    parser.add_argument("--cost", type=float, help="成本价")
    parser.add_argument("--shares", type=int, help="持仓数量")
    parser.add_argument("--date", type=str, help="买入日期 YYYY-MM-DD")
    parser.add_argument("--stop-loss", type=float, default=None, help="止损价")
    parser.add_argument("--take-profit", type=float, default=None, help="止盈价")
    parser.add_argument("--csv", type=str, help="CSV 文件路径（用于 import）")
    parser.add_argument("--field", type=str, help="更新字段名（用于 update）")
    parser.add_argument("--value", type=str, help="更新字段值（用于 update）")

    args = parser.parse_args()

    # ── 回测模式 ──
    if args.backtest:
        from backtesting import BacktestEngine

        codes = args.codes or list(STOCKS.keys())
        logger.info(f"回测模式: 策略={args.strategy}, 股票={codes}, "
                     f"区间={args.start}~{args.end or '今天'}, 复权={args.fq}")

        engine = BacktestEngine(
            strategy=args.strategy,
            codes=codes,
            start_date=args.start,
            end_date=args.end,
            fq=args.fq,
            initial_cash=args.cash,
        )

        try:
            result = engine.run()
        except ValueError as e:
            logger.error(f"回测失败: {e}")
            sys.exit(1)

        # 输出结果摘要
        print("\n" + "=" * 50)
        print("  回测结果")
        print("=" * 50)
        print(f"策略:     {result['config']['strategy']}")
        print(f"股票:     {', '.join(result['config']['codes'])}")
        print(f"区间:     {result['config']['start']} ~ {result['config']['end']}")
        print(f"初始资金: {result['config']['initial_cash']:,.0f}")
        print(f"成交笔数: {len(result['trades'])}")
        if result["equity_curve"]:
            first = result["equity_curve"][0]
            last = result["equity_curve"][-1]
            pnl = last["total_value"] - result["config"]["initial_cash"]
            pct = pnl / result["config"]["initial_cash"] * 100
            print(f"最终资金: {last['total_value']:,.2f}")
            print(f"总收益:   {pnl:+,.2f} ({pct:+.2f}%)")
        print("=" * 50)

        # 输出最近 10 笔交易
        if result["trades"]:
            print("\n最近 10 笔交易:")
            for t in result["trades"][-10:]:
                tag = "买入" if t["action"] == "buy" else "卖出"
                print(f"  {t['date']} {tag} {t['code']} "
                      f"{t['shares']}股 @{t['price']:.2f} "
                      f"手续费={t['fee']:.2f} ({t['reason']})")

        # 保存完整结果到 JSON
        output_file = os.path.join(
            PROJECT_ROOT, "data",
            f"backtest_{args.strategy}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        )
        os.makedirs(os.path.dirname(output_file), exist_ok=True)
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2, default=str)
        logger.info(f"完整回测结果已保存: {output_file}")
        sys.exit(0)

    # ── 组合管理子命令处理 ───────────────────────────────────
    if args.portfolio:
        pm = PortfolioManager()

        def _print_json(data):
            """美化打印 JSON 数据。"""
            print(json.dumps(data, ensure_ascii=False, indent=2, default=str))

        try:
            if args.portfolio == "list":
                portfolios = pm.list_portfolios()
                if not portfolios:
                    print("暂无组合")
                else:
                    print(f"共 {len(portfolios)} 个组合:\n")
                    for pf in portfolios:
                        print(
                            f"  📁 {pf['name']}  —  {pf['position_count']} 只持仓"
                        )
                        if pf.get("description"):
                            print(f"     {pf['description']}")
                        if pf.get("created_at"):
                            print(f"     创建于 {pf['created_at']}")
                        print()

            elif args.portfolio == "create":
                if not args.name:
                    print("❌ 请指定组合名称: --name")
                    sys.exit(1)
                pf = pm.create_portfolio(args.name, description=args.desc)
                print(f"✅ 组合 '{pf['name']}' 创建成功")
                print(f"   描述: {pf.get('description', '-')}")
                print(f"   创建日期: {pf['created_at']}")

            elif args.portfolio == "delete":
                if not args.name:
                    print("❌ 请指定组合名称: --name")
                    sys.exit(1)
                pm.delete_portfolio(args.name)
                print(f"✅ 组合 '{args.name}' 已删除")

            elif args.portfolio == "show":
                if not args.name:
                    print("❌ 请指定组合名称: --name")
                    sys.exit(1)
                pf = pm.get_portfolio(args.name)
                positions = pf.get("positions", [])
                total_cost = sum(
                    p["cost_price"] * p["shares"] for p in positions
                )
                print(f"📁 组合: {pf['name']}")
                if pf.get("description"):
                    print(f"   描述: {pf['description']}")
                print(f"   创建于: {pf.get('created_at', '-')}")
                print(f"   持仓数: {len(positions)}")
                print(f"   总成本: ¥{total_cost:,.2f}")
                if positions:
                    print(f"\n   {'代码':<8} {'名称':<12} {'成本':>8} {'数量':>8} {'买入日期':<12} {'止损':>8} {'止盈':>8}")
                    print(f"   {'-'*72}")
                    for p in positions:
                        sl = f"{p.get('stop_loss',''):>8}" if p.get("stop_loss") else "       -"
                        tp = f"{p.get('take_profit',''):>8}" if p.get("take_profit") else "       -"
                        print(
                            f"   {p['code']:<8} {p['name']:<12} "
                            f"{p['cost_price']:>8.2f} {p['shares']:>8} "
                            f"{p['buy_date']:<12} {sl} {tp}"
                        )
                else:
                    print("\n   (暂无持仓)")

            elif args.portfolio == "add":
                if not all([args.name, args.code, args.name2, args.cost, args.shares]):
                    print("❌ 缺少参数。用法: --portfolio add --name <组合> --code <代码> --name2 <股票名> --cost <成本> --shares <数量>")
                    sys.exit(1)
                pos = pm.add_position(
                    portfolio=args.name,
                    code=args.code,
                    name=args.name2,
                    cost_price=args.cost,
                    shares=args.shares,
                    buy_date=args.date or datetime.now().strftime("%Y-%m-%d"),
                    stop_loss=args.stop_loss,
                    take_profit=args.take_profit,
                )
                print(f"✅ 已添加持仓: {pos['name']}({pos['code']})")
                print(f"   成本: ¥{pos['cost_price']}  ×  {pos['shares']} 股")
                print(f"   买入日期: {pos['buy_date']}")
                if "stop_loss" in pos:
                    print(f"   止损价: ¥{pos['stop_loss']}")
                if "take_profit" in pos:
                    print(f"   止盈价: ¥{pos['take_profit']}")

            elif args.portfolio == "remove":
                if not all([args.name, args.code]):
                    print("❌ 缺少参数。用法: --portfolio remove --name <组合> --code <代码>")
                    sys.exit(1)
                pm.remove_position(args.name, args.code)
                print(f"✅ 已从组合 '{args.name}' 移除 '{args.code}'")

            elif args.portfolio == "update":
                if not all([args.name, args.code, args.field]):
                    print("❌ 缺少参数。用法: --portfolio update --name <组合> --code <代码> --field <字段> --value <值>")
                    print("   可更新字段: name, cost_price, shares, buy_date, stop_loss, take_profit")
                    sys.exit(1)
                # 尝试自动转换 value 类型
                val = args.value
                if args.field in ("cost_price", "stop_loss", "take_profit"):
                    val = float(args.value)
                elif args.field == "shares":
                    val = int(args.value)
                pos = pm.update_position(args.name, args.code, **{args.field: val})
                print(f"✅ 已更新 {pos['name']}({pos['code']}) 的 {args.field} → {val}")

            elif args.portfolio == "import":
                if not all([args.name, args.csv]):
                    print("❌ 缺少参数。用法: --portfolio import --name <组合> --csv <路径>")
                    sys.exit(1)
                count = pm.import_csv(args.name, args.csv)
                print(f"✅ 已从 CSV 导入 {count} 条持仓到组合 '{args.name}'")

            elif args.portfolio == "pnl":
                from portfolio.tracker import PortfolioTracker

                tracker = PortfolioTracker(pm)
                pnl_data = tracker.calculate_pnl()
                report = PortfolioTracker.format_pnl_report(pnl_data)
                print(report)

                # 检查止损/止盈触发
                triggers = tracker.check_stop_conditions(pnl_data)
                if triggers:
                    print(f"\n⚠️  共 {len(triggers)} 条止损/止盈触发！")
                    for t in triggers:
                        emoji = "🔴" if t["type"] == "stop_loss" else "🟢"
                        type_cn = "止损" if t["type"] == "stop_loss" else "止盈"
                        print(
                            f"  {emoji} [{t['portfolio']}] {t['name']}({t['code']}) "
                            f"{type_cn}线 ¥{t['trigger_price']:.2f} — "
                            f"现价 ¥{t['current_price']:.2f}"
                        )

        except ValueError as e:
            print(f"❌ {e}")
            sys.exit(1)
        except FileNotFoundError as e:
            print(f"❌ {e}")
            sys.exit(1)

        sys.exit(0)

    # ── 盘中/单次模式 ──
    if args.once or args.now:
        # 单次执行模式
        if not is_trading_time() and not args.now:
            logger.info("当前非交易时间，使用 --now 可强制运行")
            sys.exit(0)
        # 加载成交量基准（单次模式也需要）
        try:
            _daily_baselines = load_daily_baselines(list(STOCKS.keys()))
            _baselines_loaded_today = datetime.now().date()
            logger.info(f"成交量基准已加载: {len(_daily_baselines)} 只股票")
        except Exception as e:
            logger.warning(f"成交量基准加载失败: {e}")
        # 加载换手率基准（单次模式也需要）
        try:
            _turnover_baselines = load_turnover_baselines(list(STOCKS.keys()))
            _turnover_loaded_today = datetime.now().date()
            logger.info(f"换手率基准已加载: {len(_turnover_baselines)} 只股票")
        except Exception as e:
            logger.warning(f"换手率基准加载失败: {e}")
        run_once()
        # --now 模式下也执行收盘后任务
        if args.now:
            logger.info("── 收盘后调度开始 ──")
            results = _after_close.run_pending()
            if results:
                for name, result in results:
                    logger.info(f"  收盘后任务 [{name}] 完成: {result}")
            else:
                logger.info("  无待执行任务")
        sys.exit(0)

    # 守护模式
    run_loop()
