# B-3 回测报告（图表）— 完整规格

## 改动内容
新建 `backtesting/reporter.py`：
1. `generate_report(result: dict) -> str` — 输入 `BacktestEngine.run()` + `calculate_metrics()` 的输出，输出 HTML 文件路径
2. 图表内容（matplotlib 生成 → base64 嵌入 HTML）：
   - 权益曲线（equity curve + buy/sell 标记）
   - 回撤曲线（drawdown 面积图）
   - 月度收益率热力图（12×N 网格）
   - 交易盈亏分布直方图（每笔收益率分布）
3. 数据表格：metrics 摘要表（收益/回撤/夏普/胜率/盈亏比等 10 项）
4. HTML 模板：自带 CSS 样式，移动端适配，可直接浏览器打开
5. 保存到 `data/reports/backtest_<策略>_<日期>.html`

## 验收标准
- [ ] `generate_report()` 接收有交易的回测结果 → 生成完整 HTML
- [ ] HTML 包含 4 张图表 + metrics 表格
- [ ] matplotlib 图表正确嵌入（base64，不依赖外部图片文件）
- [ ] 空交易回测（无信号）→ 生成简约版报告（只含文字说明，无图表报错）
- [ ] HTML 可独立在浏览器打开（无跨域/路径依赖）
- [ ] 目录 `data/reports/` 不存在时自动创建

## 依赖
- matplotlib（pandas 已带）
- 输入格式：`BacktestEngine.output()` 的 dict 格式 + `calculate_metrics()` 输出
