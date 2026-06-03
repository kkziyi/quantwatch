#!/bin/bash
# QuantWatch Cron Wrapper
# 由 cron job 每 5 分钟调用，盘中获取行情并推送
# 
# 用法: bash cron_check.sh

set -e

PROJECT_DIR="/mnt/d/MyProject/stock-watch"
cd "$PROJECT_DIR"

# 非交易时间静默退出（main.py --once 内部也会判断，这里做前置优化）
HOUR=$(date +%H)
MINUTE=$(date +%M)
DAY=$(date +%u)  # 1=周一, 5=周五, 6=周六, 7=周日

if [ "$DAY" -gt 5 ]; then
    # 周末不运行
    exit 0
fi

TIME_NOW=$((10#$HOUR * 100 + 10#$MINUTE))
TIME_START=925
TIME_END=1505

if [ "$TIME_NOW" -lt "$TIME_START" ] || [ "$TIME_NOW" -gt "$TIME_END" ]; then
    # 非交易时间不运行
    exit 0
fi

# 执行单次轮询
python3 main.py --now
