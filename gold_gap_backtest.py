# -*- coding: utf-8 -*-
"""黄金 CME 日线缺口反转策略回测
- 数据: PAXGUSDT 1h, 按 COMEX 交易日合成日线(北京06:00开盘 = UTC前日22:00)
- 严格缺口: 今日开盘跳过昨日最高(向上)/最低(向下) -- 真实日线缺口定义
- 隔夜缺口: 今日开盘跳离昨日收盘(--gap-mode overnight, 覆盖COMEX休市一小时的隔夜重定价)
- 上方未回补缺口: 价格涨入缺口区 -> 做空; 下方未回补缺口: 价格跌入缺口区 -> 做多
- 止损 = 最近10根1h对数收益 sigma 的 2 倍; 止盈 = 5倍(1:5)
- 本金1000U, 每仓100U保证金x100x全仓, 手续费0.04%/边, 资金费0.01%/8h, 强平约-9.5%
"""
import sys
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

sys.path.insert(0, ".trae/skills/btc-eth-macd-backtest/scripts")
import macd_backtest as bt

INIT_BAL = 1000.0
MARGIN = 100.0
LEV = 100.0
NOTIONAL = MARGIN * LEV          # 10000U
FEE = 0.0004                     # 单边
FUNDING_1H = 0.0001 / 8          # 每根1h
LIQ_PCT = 0.095                  # 全仓强平距离(相对开仓价)
SIGMA_BARS = 10                  # 波动率窗口
SIGMA_K = 2.0                    # 止损 = 2 sigma
RR = 5.0                         # 盈亏比 1:5
DAYS = 90
GAP_MODE = "overnight"
if "--gap-mode" in sys.argv:
    GAP_MODE = sys.argv[sys.argv.index("--gap-mode") + 1]

df = bt.fetch_klines("PAXGUSDT", "1h", DAYS)
df["tday"] = (df.index + pd.Timedelta(hours=2)).date   # COMEX交易日标签(北京06:00开盘)

# ---- COMEX 交易日日线(PAXG尺度) ----
daily = df.groupby("tday").agg(
    o=("open", "first"), h=("high", "max"), l=("low", "min"), c=("close", "last"))

# ---- 缺口识别 ----
zones = []   # top/bottom, kind: res=上方阻力区做空 / sup=下方支撑区做多, born
for i in range(1, len(daily)):
    d0 = daily.index[i]
    o, ph, pl, pc = daily.iloc[i]["o"], daily.iloc[i - 1]["h"], daily.iloc[i - 1]["l"], daily.iloc[i - 1]["c"]
    if GAP_MODE == "strict":
        gap_up, gap_dn = o > ph, o < pl
        up_zone, dn_zone = (ph, o), (o, pl)
    else:
        gap_up, gap_dn = o > pc, o < pc
        up_zone, dn_zone = (pc, o), (o, pc)
    if gap_up:
        zones.append(dict(top=max(up_zone), bottom=min(up_zone), kind="res", born=d0,
                          filled=False, used=False))
    elif gap_dn:
        zones.append(dict(top=max(dn_zone), bottom=min(dn_zone), kind="sup", born=d0,
                          filled=False, used=False))
print("日线 {} 天 ({}模式), 识别缺口 {} 个".format(len(daily), GAP_MODE, len(zones)))

def zone_of(price_hi, price_lo, cur_tday):
    """返回价格区间触碰到的活跃缺口(已形成、未回补、未用过)"""
    for z in zones:
        if z["filled"] or z["used"] or cur_tday < z["born"]:
            continue
        if price_lo <= z["top"] and price_hi >= z["bottom"]:
            return z
    return None

# ---- 逐根回测 ----
bal = INIT_BAL
pos = None
trades = []
equity = []
sl_dist = None
sig_hist = np.log(df["close"]).diff()

for i in range(1, len(df)):
    bar, prev_c = df.iloc[i], df.iloc[i - 1]["close"]
    ts = df.index[i]

    # 持仓: 更新浮盈、资金费、检查出场
    if pos is not None:
        px = bar["close"]
        sign = 1 if pos["side"] == "long" else -1
        pos["funding"] += NOTIONAL * FUNDING_1H
        pnl_px = sign * (px - pos["entry"]) / pos["entry"] * NOTIONAL
        sl_hit = (pos["side"] == "long" and bar["low"] <= pos["sl"]) or \
                 (pos["side"] == "short" and bar["high"] >= pos["sl"])
        tp_hit = (pos["side"] == "long" and bar["high"] >= pos["tp"]) or \
                 (pos["side"] == "short" and bar["low"] <= pos["tp"])
        liq_hit = pnl_px <= -LIQ_PCT * NOTIONAL
        exit_px = exit_kind = None
        if pos["side"] == "long":
            gap_open = bar["open"] <= pos["sl"]
        else:
            gap_open = bar["open"] >= pos["sl"]
        if gap_open:                      # 开盘跳空越过止损 -> 按开盘价
            exit_px, exit_kind = bar["open"], "跳空止损"
        elif liq_hit and not (sl_hit or tp_hit):
            exit_px, exit_kind = pos["entry"] * (1 - sign * LIQ_PCT), "强平"
        elif sl_hit and tp_hit:           # 同根都触 -> 近者先
            d_sl = abs(pos["entry"] - pos["sl"]) / pos["entry"]
            d_tp = abs(pos["tp"] - pos["entry"]) / pos["entry"]
            if d_sl <= d_tp:
                exit_px, exit_kind = pos["sl"], "止损"
            else:
                exit_px, exit_kind = pos["tp"], "止盈"
        elif sl_hit:
            exit_px, exit_kind = pos["sl"], "止损"
        elif tp_hit:
            exit_px, exit_kind = pos["tp"], "止盈"
        if exit_px is not None:
            pnl = sign * (exit_px - pos["entry"]) / pos["entry"] * NOTIONAL
            pnl -= NOTIONAL * FEE * 2 + pos["funding"]        # 开平双边手续费+资金费
            bal += pnl
            trades.append(dict(t_in=pos["t_in"], side=pos["side"], entry=pos["entry"],
                               t_out=ts, exit=exit_px, kind=exit_kind, pnl=pnl, bal=bal,
                               sig=pos["sigma"]))
            pos = None
        equity.append(bal + (pnl_px if pos is not None else 0))
    else:
        equity.append(bal)

    # 缺口回补检测(已回补的zone移除)
    for z in zones:
        if z["filled"] or z["used"]:
            continue
        if z["kind"] == "res" and bar["low"] <= z["bottom"]:
            z["filled"] = True
        elif z["kind"] == "sup" and bar["high"] >= z["top"]:
            z["filled"] = True

    # 无持仓 -> 信号检测(1h收盘进入缺口区)
    if pos is None:
        z = zone_of(bar["high"], bar["low"], bar["tday"])
        if z is not None:
            c = bar["close"]
            side = "short" if z["kind"] == "res" else "long"
            sig = float(sig_hist.iloc[i - SIGMA_BARS + 1: i + 1].std())
            sl_d = SIGMA_K * sig
            sign = 1 if side == "long" else -1
            pos = dict(side=side, entry=c, sl=c * (1 - sign * sl_d),
                       tp=c * (1 + sign * sl_d * RR), t_in=ts, funding=0.0, sigma=sig)
            z["used"] = True

eq = pd.Series(equity, index=df.index[1:])
closed = [t for t in trades]
final = bal if pos is None else bal
open_pnl = 0.0
if pos is not None:
    sign = 1 if pos["side"] == "long" else -1
    open_pnl = sign * (df["close"].iloc[-1] - pos["entry"]) / pos["entry"] * NOTIONAL
    final += 0  # 已实现口径

wins = [t for t in closed if t["pnl"] > 0]
losses = [t for t in closed if t["pnl"] <= 0]
gp = sum(t["pnl"] for t in wins)
gl = abs(sum(t["pnl"] for t in losses))
dd = (eq / eq.cummax() - 1).min() if len(eq) else 0

print("=" * 72)
print("黄金 CME 日线缺口反转策略回测 (PAXGUSDT, {} 天)".format(DAYS))
print("规则: 涨入上方未回补缺口做空 / 跌入下方未回补缺口做多")
print("止损=近{}根1h 2sigma  止盈=5倍  100U x 100x 全仓".format(SIGMA_BARS))
print("=" * 72)
print("识别缺口 {} 个, 触发交易 {} 笔".format(len(zones), len(closed)))
if closed:
    print("\n交易明细:")
    print("入场时间            方向    入场价     出场时间           出场价    结果      盈亏U     余额U    止损%")
    for t in closed:
        print("{} {:<4s} {:>9.2f} -> {} {:>9.2f}  {:<5s} {:>8.2f} {:>8.2f}  {:.2f}%".format(
            str(t["t_in"]), t["side"], t["entry"], str(t["t_out"]), t["exit"],
            t["kind"], t["pnl"], t["bal"], t["sig"] * SIGMA_K * 100))
    print("\n止盈 {} / 止损 {} / 强平 {}".format(
        sum(1 for t in closed if t["kind"] == "止盈"),
        sum(1 for t in closed if t["kind"] == "止损"),
        sum(1 for t in closed if t["kind"] == "强平")))
    print("胜率 {:.1f}%  利润因子 {:.2f}  盈亏比 {:.2f}".format(
        100 * len(wins) / len(closed),
        gp / gl if gl > 0 else float("inf"),
        (gp / len(wins)) / (gl / len(losses)) if wins and losses else 0))
if pos is not None:
    print("\n未平仓: {} @ {:.2f}  浮盈 {:.2f}U".format(pos["side"], pos["entry"], open_pnl))
print("\n期末权益(已实现): {:.2f}U ({:+.2f}%)".format(bal, (bal / INIT_BAL - 1) * 100))
if pos is not None:
    print("加浮盈后约: {:.2f}U ({:+.2f}%)".format(bal + open_pnl, ((bal + open_pnl) / INIT_BAL - 1) * 100))
print("最大回撤: {:.2f}%".format(dd * 100))

# ---- 图 ----
fig, ax = plt.subplots(2, 1, figsize=(15, 9), sharex=True,
                       gridspec_kw=dict(height_ratios=[3, 1]))
ax[0].plot(df.index, df["close"], lw=0.8, color="#333")
for z in zones:
    col = "#ff9999" if z["kind"] == "res" else "#99cc99"
    ax[0].fill_between(df.index, z["bottom"], z["top"], color=col, alpha=0.35)
for t in closed:
    m, c = ("^", "red") if t["side"] == "long" else ("v", "green")
    ax[0].scatter(t["t_in"], t["entry"], marker=m, color=c, s=90, zorder=5)
    ax[0].annotate("{}".format(t["kind"]), (t["t_out"], t["exit"]), fontsize=7,
                   xytext=(0, 6), textcoords="offset points")
ax[0].set_title("PAXG gap-fill reversal | 2σ stop / 1:5 TP | red=long green=short | shaded=gaps")
ax[1].plot(eq.index, eq.values, color="tab:blue", lw=1)
ax[1].axhline(INIT_BAL, ls="--", c="gray", lw=0.6)
ax[1].set_ylabel("Equity U")
plt.tight_layout()
plt.savefig("gold_gap_backtest_result.png", dpi=130)
print("\n图: gold_gap_backtest_result.png")
