# crypto-strategy-monitor

BTC / ETH / 黄金(PAXG) 合约策略回测、实时监控与可视化面板（模拟盘）。

## 策略概览

- **MACD 二次交叉**（BTC/ETH）：零轴水下金叉→死叉→金叉（金叉2 DIF 更高）做多，水上死叉→金叉→死叉（死叉2 DIF 更低）做空；支持 1h / 4h 多周期共享资金池。
- **黄金关键位反转 + MACD 序列确认**（PAXG，1h）：价格触及日线摆动高/低点 ±0.15%，且 1h MACD 出现对应三叉序列时反向开仓；止损 = max(近10根 2σ, 0.15%)，止盈 1:3。
- **宏观监控**：美债 2Y/5Y/10Y/30Y 收益率与利差、DXY、USDJPY，阈值穿越告警。
- **可视化面板**：`strategy_dashboard.py`，本地网页面板（localhost:8765），展示权益、持仓、形态进度、关键位、成交流水。

## 模拟盘口径

本金 1000U/账户，单笔 100U 保证金 × 100x 全仓模拟；手续费 0.04%/边，资金费 0.01%/8h；K 线内出场按"止损/强平谁离入场更近先触"判定，开盘跳空按滑点处理。

## 数据

K 线来自 Binance 公共数据镜像（现货价格模拟合约）；宏观数据来自东财数据中心 / 新浪财经接口。

## 主要文件

| 文件 | 说明 |
|---|---|
| `.trae/skills/btc-eth-macd-backtest/` | MACD 二次交叉策略回测技能（取数/信号/资金模型） |
| `macd_signal_monitor.py` | BTC/ETH 多周期实时监控 + 共享资金池模拟盘 |
| `gold_swing_backtest.py` | 黄金关键位反转策略回测（参数：周期、盈亏比） |
| `gold_swing_monitor.py` | 黄金策略实时监控 |
| `macro_monitor.py` | 宏观数据监控与告警 |
| `strategy_dashboard.py` | 可视化网页面板 |
| `generate_strategy_report.py` | 策略运行报告生成器 |
| `gate_trader.py` | Gate.io 实盘模块（默认未启用） |

## 安全

密钥类文件（`*_config.json`）、运行状态（`paper_state*.json`）、日志（`*.log`）均通过 `.gitignore` 排除，不纳入版本库。

> 仅供学习研究，不构成投资建议。
