# crypto-strategy-monitor

BTC / ETH / 黄金(PAXG) 合约策略回测、实时行情监控、套利与市场情绪可视化系统（模拟盘）。

## 系统架构（模块总览）

| 模块 | 状态 | 入口 |
|---|---|---|
| 策略模块（回测 + 信号） | ✅ 已上线 | `.trae/skills/`、`*_backtest.py` |
| 行情监控系统（加密） | ✅ 已上线 | `macd_signal_monitor.py`、`gold_swing_monitor.py` |
| 行情监控（宏观） | ✅ 已上线 | `macro_monitor.py` |
| 可视化面板 | ✅ 已上线 | `strategy_dashboard.py` |
| 套利模块 | 🚧 接口已验证 | `coinglass_probe.py` |
| 恐贪指数 | 🚧 接入规划 | Coinglass `/api/index/fear-greed-history` |
| 筹码峰 | 🚧 接入规划 | Coinglass 订单簿/清算热力图端点 |

---

## 一、策略模块

回测、信号生成与模拟盘成交共用同一套资金/出场口径，保证回测与监控结果可横向对比。

- **MACD 二次交叉（BTC/ETH）**：零轴水下金叉→死叉→金叉（金叉2 的 DIF 高于金叉1）做多；水上死叉→金叉→死叉（死叉2 的 DIF 低于死叉1）做空。支持 1h / 4h 多周期，多信号流共享一个资金池，空闲保证金不足时新信号自动跳过。
- **黄金关键位反转 + MACD 序列确认（PAXG，1h）**：价格触及日线摆动高/低点 ±0.15%，且 1h MACD 完成对应三叉序列（触支撑：金叉→死叉→金叉做多；触阻力：死叉→金叉→死叉做空）；止损 = max(近10根 K 线 2σ, 0.15%)，止盈 1:3，关键位有效期 15 个交易日。
- **回测覆盖**：BTC/ETH 的 1h/4h/日线多周期、1:1 ~ 1:5 盈亏比、固定/波动率定标止损；黄金的 MACD 二次交叉全系参数与关键位反转系列。

## 二、行情监控系统（加密货币）

- 每根 K 线收盘后 8 秒检查，`closed_bars()` 剔除未收盘 K 线；形态状态机逐根重放，重启不丢信号。
- 多标的多周期并行：ETH 1h(1%/2%)、ETH 4h(3%/3%)、BTC 4h(3%/3%) 共享 1000U 资金池；黄金独立 1000U 账户。
- 关键位跨重启持久化（日线摆动高低点按交易日聚合，去重/失效管理）。
- 告警：本机蜂鸣 + `signal_alerts.log` + PushPlus 微信群推送（每轮巡检推送状态，买卖点信号额外推送）。
- 模拟盘：止损/止盈/强平/资金费/手续费完整模拟，K 线内出场按"离入场更近者先触"判定。

## 三、行情监控（宏观）

`macro_monitor.py`，60 秒轮询：

- 美债 2Y / 5Y / 10Y / 30Y 收益率、10Y-2Y 利差（东财数据中心，日度，本地缓存）。
- 美元指数 DXY、USDJPY（新浪财经接口）。
- 告警口径：美债 2Y/10Y/30Y 日变动 ≥5bp、利差 ≥8bp、DXY/USDJPY 日内 ≥0.5%（按倍数阶梯）、USDJPY 关键位穿越；同阶梯/关键位同日仅告警一次；每日 08:00 后推送宏观日报。

## 四、套利模块（接入中）

基于 Coinglass V4 开放 API（`open-api-v4.coinglass.com`），已完成端点与鉴权链路验证（密钥本地配置、不入库）：

- **资金费率套利**：`/api/futures/funding-rate/arbitrage`（套利机会）、`/funding-rate/oi-weight-history`（OI 加权费率）、`/funding-rate/exchange-list`（各所费率）。
- 现货-合约基差：`/api/futures/basis/history`。
- 待 Coinglass 套餐开通数据权限后接入信号与推送。

## 五、恐贪指数（接入规划）

- 数据源：Coinglass `/api/index/fear-greed-history`（Crypto Fear & Greed Index 历史）。
- 用途：作为 BTC/ETH MACD 信号与黄金反转信号的**情绪过滤层**（极度恐惧不追空、极度贪婪不追多），并在面板与微信群日报中展示。

## 六、筹码峰（接入规划）

- 数据源（Coinglass V4）：
  - 订单簿历史/热力图：`/api/futures/orderbook/history`、`/aggregated-ask-bids-history`
  - 清算热力图与最大痛点：`/api/futures/liquidation/heatmap/model1~3`、`/liquidation/max-pain`
  - 大单挂单：`/api/futures/orderbook/large-limit-order`
- 用途：识别密集成交区/大单支撑阻力、清算聚集价位，与黄金关键位策略、面板阻力支撑标注联动。

## 七、可视化面板

`strategy_dashboard.py`，纯标准库 HTTP 服务（无额外依赖），浏览器访问 `http://localhost:8765`，20 秒自动刷新：

- 两个模拟账户权益/收益率/余额轨迹、实时浮盈与持仓明细。
- MACD 三条信号流的 DIF、水上/水下、完整形态进度（买卖点闪烁）。
- 黄金活跃阻力/支撑关键位、MACD 三叉序列、成交流水、信号提醒。
- 行情/形态计算带缓存（行情 10s、形态 60s），不给交易所接口与监控进程增压。

## 模拟盘口径

本金 1000U/账户，单笔 100U 保证金 × 100x 全仓模拟；手续费 0.04%/边，资金费 0.01%/8h 按周期折算；K 线内出场按"止损/强平谁离入场更近先触"判定，开盘跳空按滑点处理；全仓下强平价随账户权益动态变化，逐笔比较距离。

## 数据源

- K 线：Binance 公共数据镜像 `data-api.binance.vision`（现货价格模拟合约，PAXG 仅现货行情）。
- 宏观：东方财富数据中心、新浪财经。
- 套利/情绪/筹码：Coinglass V4（需自备 API Key）。

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
| `coinglass_probe.py` | Coinglass V4 端点/鉴权探测（套利、恐贪、筹码） |
| `gate_trader.py` | Gate.io 实盘模块（默认未启用） |

## 安全

密钥类文件（`*_config.json`，如 `coinglass_config.json`、`wechat_config.json`、`gate_config.json`）、运行状态（`paper_state*.json`、`macro_state.json`）、日志（`*.log`）均通过 `.gitignore` 排除，不纳入版本库；代码仅从配置文件或环境变量读取密钥。

> 仅供学习研究，不构成投资建议。
