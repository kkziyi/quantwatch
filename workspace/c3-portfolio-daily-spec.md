# C-3 组合日报 — 完整规格

## 改动内容
新建 `portfolio/daily_report.py`：
1. `PortfolioDailyReporter`: 从 PortfolioManager 读取持仓 → AKShare 获取最新价 → 计算每只收益 + 总收益
2. `generate_daily_brief() -> str`: 生成飞书格式的文本/卡片内容
3. 注册到 AfterCloseScheduler（order=60，排在每日复盘之后）
4. 飞书推送示例格式：
   ```
   📋 组合日报 2026-06-01
   
   持仓 3 只  总市值 ¥1,023,456  总收益 +2.34%
   
   🟢 中国巨石 600176  成本 12.50  现价 13.80  +10.40%
   🔴 东方电气 600875  成本 18.00  现价 17.50  -2.78%
   🟢 应流股份  成本 22.00  现价 23.10  +5.00%
   ```

## 验收标准
- [ ] 从 PortfolioManager 读取当前持仓
- [ ] AKShare 获取最新收盘价（复用策略模块已有的数据获取方式）
- [ ] 正确计算每只持仓的成本、现价、收益率
- [ ] 正确计算组合总市值、总收益率
- [ ] 飞书推送格式美观，正收益🟢负收益🔴一目了然
- [ ] 注册到 AfterCloseScheduler order=60
- [ ] 空持仓时推送："📋 组合日报 — 当前无持仓"
- [ ] 周末自动跳过

## 依赖
- portfolio/manager.py（PortfolioManager）
- schedulers/after_close.py（AfterCloseScheduler）
- notifiers/feishu.py（飞书发送）
