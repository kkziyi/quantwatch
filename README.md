# QuantWatch — A 股量化监控系统

A 股智能监控工具，盘中自动轮询自选股行情，异常波动实时推送飞书。

## 项目状态
📌 第一阶段：涨跌提醒系统（建设中）

### 已接入自选股
| 代码 | 名称 | 涨跌阈值 |
|------|------|---------|
| 600176 | 中国巨石 | ±3% |
| 600875 | 东方电气 | ±3% |
| 603308 | 应流股份 | ±3% |
| 000636 | 风华高科 | ±3% |
| 002353 | 杰瑞股份 | ±3% |

### 计划路线
- [ ] Phase 1: 涨跌提醒系统
- [ ] Phase 2: 技术指标信号（MACD/KDJ）
- [ ] Phase 3: 策略回测
- [ ] Phase 4: Dashboard 看板

## 项目结构
```
stock-watch/
├── config.py         # 配置：自选股、阈值等
├── main.py           # 入口 + 调度逻辑
├── strategies/       # 策略规则
│   └── price_alert.py
├── notifiers/        # 通知模块
│   └── feishu.py
├── data/             # 数据缓存
├── docs/
│   └── README.md
└── requirements.txt
```

## 技术栈
- Python 3.11+
- AKShare（行情数据）
- Hermes cronjob（定时调度）
- 飞书通知
