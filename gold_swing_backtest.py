# -*- coding: utf-8 -*-
"""黄金 前高/前低关键位反转 + MACD序列确认 策略回测
- 数据: PAXGUSDT K线, 信号周期由命令行参数指定(默认1h, 可选4h), 按COMEX交易日(北京06:00开盘)合成日线
- 关键位: 日线摆动高点/低点(左右各2根), 容差带±0.15%, 有效期15交易日
- 进场确认(信号周期 MACD 12/26/9 交叉序列):
  触及下方支撑位 且 最近三次交叉=金叉→死叉→金叉 -> 做多
  触及上方阻力位 且 最近三次交叉=死叉→金叉→死叉 -> 做空
  (序列未完成则不开仓, 价格留在容差带内等待, 关键位过期作废)
- 止损 = 最近10根信号周期K线对数收益 sigma 的 2 倍; 止盈 = 3倍(1:3)
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
NOTIONAL = MARGIN * LEV
FEE = 0.0004
FUNDING_1H = 0.0001 / 8
LIQ_PCT = 0.095
SIGMA_BARS = 10
SIGMA_K = 2.0
SL_FLOOR = 0.0015                 # 止损下限(防止流动性真空期σ→0产生超窄止损)
DAYS = 90
SWING_N = 2        # 摆动结构左右各2根日线
TOL = 0.0015       # 关键位容差带 ±0.15%
MAX_AGE = 15       # 关键位有效期(交易日)
DEDUP = 0.003      # 触发后相邻同向关键位一并失效的范围

TF = sys.argv[1] if len(sys.argv) > 1 else "1h"
RR = float(sys.argv[2]) if len(sys.argv) > 2 else 3.0
HOURS = {"1h": 1, "4h": 4}[TF]
FUNDING_BAR = FUNDING_1H * HOURS   # 资金费0.01%/8h 按K线周期折算

df = bt.fetch_klines("PAXGUSDT", TF, DAYS)
df["tday"] = (df.index + pd.Timedelta(hours=2)).date
daily = df.groupby("tday").agg(o=("open", "first"), h=("high", "max"),
                               l=("low", "min"), c=("close", "last"))

# ---- 摆动关键位 ----
levels = []
for i in range(SWING_N, len(daily) - SWING_N):
    seg_h = daily["h"].iloc[i - SWING_N:i + SWING_N + 1]
    seg_l = daily["l"].iloc[i - SWING_N:i + SWING_N + 1]
    d0 = daily.index[i]
    if daily["h"].iloc[i] == seg_h.max():
        levels.append(dict(price=daily["h"].iloc[i], kind="res", born=d0,
                           used=False, dead=False))
    if daily["l"].iloc[i] == seg_l.min():
        levels.append(dict(price=daily["l"].iloc[i], kind="sup", born=d0,
                           used=False, dead=False))
print("日线 {} 天, 识别摆动关键位 {} 个(阻力 {} / 支撑 {})".format(
    len(daily), len(levels),
    sum(1 for x in levels if x["kind"] == "res"),
    sum(1 for x in levels if x["kind"] == "sup")))


def level_touch(hi, lo, cur_tday):
    """返回被触碰的活跃关键位(已形成/未用/未失效/未过期)"""
    for lv in levels:
        if lv["used"] or lv["dead"] or cur_tday < lv["born"]:
            continue
        age = (cur_tday - lv["born"]).days
        if age > MAX_AGE:
            continue
        top, bot = lv["price"] * (1 + TOL), lv["price"] * (1 - TOL)
        if lo <= top and hi >= bot:
            return lv
    return None


def deactivate_around(lv):
    """触发后把相邻同向关键位一并标记, 避免密集重复开仓"""
    for other in levels:
        if other is not lv and other["kind"] == lv["kind"] and \
                abs(other["price"] / lv["price"] - 1) < DEDUP:
            other["used"] = True
    lv["used"] = True


# ---- 1h MACD 交叉序列 ----
ema12 = df["close"].ewm(span=12, adjust=False).mean()
ema26 = df["close"].ewm(span=26, adjust=False).mean()
dif = ema12 - ema26
dea = dif.ewm(span=9, adjust=False).mean()
above = (dif > dea).tolist()
macd_events = []          # 交叉事件序列: 'G'=金叉 'D'=死叉
for i in range(1, len(df)):
    if above[i] and not above[i - 1]:
        macd_events.append(i)
    elif not above[i] and above[i - 1]:
        macd_events.append(-i)     # 负号=死叉
ev_idx = 0                # 已消费事件指针(逐根推进)

# ---- 逐根回测 ----
bal = INIT_BAL
pos = None
trades = []
equity = []
sig_hist = np.log(df["close"]).diff()

for i in range(1, len(df)):
    bar = df.iloc[i]
    ts = df.index[i]

    if pos is not None:
        px = bar["close"]
        sign = 1 if pos["side"] == "long" else -1
        pos["funding"] += NOTIONAL * FUNDING_BAR
        pnl_px = sign * (px - pos["entry"]) / pos["entry"] * NOTIONAL
        sl_hit = (pos["side"] == "long" and bar["low"] <= pos["sl"]) or \
                 (pos["side"] == "short" and bar["high"] >= pos["sl"])
        tp_hit = (pos["side"] == "long" and bar["high"] >= pos["tp"]) or \
                 (pos["side"] == "short" and bar["low"] <= pos["tp"])
        liq_hit = pnl_px <= -LIQ_PCT * NOTIONAL
        gap_open = bar["open"] <= pos["sl"] if pos["side"] == "long" \
            else bar["open"] >= pos["sl"]
        exit_px = exit_kind = None
        if gap_open:
            exit_px, exit_kind = bar["open"], "跳空止损"
        elif liq_hit and not (sl_hit or tp_hit):
            exit_px, exit_kind = pos["entry"] * (1 - sign * LIQ_PCT), "强平"
        elif sl_hit and tp_hit:
            near_sl = abs(pos["entry"] - pos["sl"]) <= abs(pos["tp"] - pos["entry"])
            exit_px, exit_kind = (pos["sl"], "止损") if near_sl else (pos["tp"], "止盈")
        elif sl_hit:
            exit_px, exit_kind = pos["sl"], "止损"
        elif tp_hit:
            exit_px, exit_kind = pos["tp"], "止盈"
        if exit_px is not None:
            pnl = sign * (exit_px - pos["entry"]) / pos["entry"] * NOTIONAL \
                - NOTIONAL * FEE * 2 - pos["funding"]
            bal += pnl
            trades.append(dict(t_in=pos["t_in"], side=pos["side"], entry=pos["entry"],
                               t_out=ts, exit=exit_px, kind=exit_kind, pnl=pnl,
                               bal=bal, sl_pct=pos["sl_pct"],
                               sl=pos["sl"], tp=pos["tp"]))
            pos = None
        equity.append(bal + (pnl_px if pos is not None else 0))
    else:
        equity.append(bal)

    # 关键位失效(收盘穿越容差带外沿)与过期
    for lv in levels:
        if lv["dead"] or lv["used"]:
            continue
        if (ts + pd.Timedelta(hours=2)).date() < lv["born"]:
            continue
        if lv["kind"] == "res" and bar["close"] > lv["price"] * (1 + TOL):
            lv["dead"] = True
        elif lv["kind"] == "sup" and bar["close"] < lv["price"] * (1 - TOL):
            lv["dead"] = True

    # 信号检测(关键位触碰 + MACD交叉序列确认)
    while ev_idx < len(macd_events) and abs(macd_events[ev_idx]) <= i:
        ev_idx += 1
    recent = macd_events[max(0, ev_idx - 3):ev_idx]
    seq_ok = [1 if e > 0 else -1 for e in recent]
    if pos is None:
        lv = level_touch(bar["high"], bar["low"], bar["tday"])
        if lv is not None:
            side = "short" if lv["kind"] == "res" else "long"
            need = [-1, 1, -1] if side == "short" else [1, -1, 1]
            if seq_ok == need:
                c = bar["close"]
                sig = float(sig_hist.iloc[i - SIGMA_BARS + 1: i + 1].std())
                sl_d = max(SIGMA_K * sig, SL_FLOOR)
                sign = 1 if side == "long" else -1
                pos = dict(side=side, entry=c, sl=c * (1 - sign * sl_d),
                           tp=c * (1 + sign * sl_d * RR), t_in=ts, funding=0.0,
                           sl_pct=sl_d)
                deactivate_around(lv)

eq = pd.Series(equity, index=df.index[1:])
wins = [t for t in trades if t["pnl"] > 0]
losses = [t for t in trades if t["pnl"] <= 0]
gp = sum(t["pnl"] for t in wins)
gl = abs(sum(t["pnl"] for t in losses))
dd = (eq / eq.cummax() - 1).min() if len(eq) else 0
open_pnl = 0.0
if pos is not None:
    sign = 1 if pos["side"] == "long" else -1
    open_pnl = sign * (df["close"].iloc[-1] - pos["entry"]) / pos["entry"] * NOTIONAL

print("=" * 72)
print("黄金 关键位反转+MACD序列确认 策略回测 (PAXGUSDT, {} 天)".format(DAYS))
print("规则: 触及上方摆动高点±0.15%且{t}死叉→金叉→死叉做空 / 触及下方摆动低点±0.15%且{t}金叉→死叉→金叉做多".format(t=TF))
print("止损=近{}根{} 2sigma  止盈={:.0f}倍(1:{:.0f})  100U x 100x 全仓  关键位有效期{}交易日".format(
    SIGMA_BARS, TF, RR, RR, MAX_AGE))
print("=" * 72)
if trades:
    print("\n交易明细:")
    print("入场时间            方向    入场价     出场时间           出场价    结果      盈亏U     余额U    止损%")
    for t in trades:
        print("{} {:<5s} {:>9.2f} -> {} {:>9.2f}  {:<5s} {:>8.2f} {:>8.2f}  {:.2f}%".format(
            str(t["t_in"]), t["side"], t["entry"], str(t["t_out"]), t["exit"],
            t["kind"], t["pnl"], t["bal"], t["sl_pct"] * 100))
    print("\n止盈 {} / 止损 {} / 强平 {}".format(
        sum(1 for t in trades if t["kind"] == "止盈"),
        sum(1 for t in trades if t["kind"] == "止损"),
        sum(1 for t in trades if t["kind"] == "强平")))
    print("胜率 {:.1f}%  利润因子 {:.2f}  盈亏比 {:.2f}".format(
        100 * len(wins) / len(trades),
        gp / gl if gl > 0 else float("inf"),
        (gp / len(wins)) / (gl / len(losses)) if wins and losses else 0))
else:
    print("\n无交易触发")
if pos is not None:
    print("\n未平仓: {} @ {:.2f}  浮盈 {:.2f}U".format(pos["side"], pos["entry"], open_pnl))
print("\n期末权益(已实现): {:.2f}U ({:+.2f}%)".format(bal, (bal / INIT_BAL - 1) * 100))
if pos is not None:
    print("加浮盈后约: {:.2f}U ({:+.2f}%)".format(bal + open_pnl,
          ((bal + open_pnl) / INIT_BAL - 1) * 100))
print("最大回撤: {:.2f}%".format(dd * 100))

# ---- 图（详细标注版） ----
plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
plt.rcParams["axes.unicode_minus"] = False

fig, ax = plt.subplots(2, 1, figsize=(17, 10.5), sharex=True,
                       gridspec_kw=dict(height_ratios=[3, 1]))
ax[0].plot(df.index, df["close"], lw=0.9, color="#333", label="PAXG收盘价", zorder=3)

# 关键位: 水平线 + 左端价位标注(线型区分状态, 减少文字重叠)
for lv in levels:
    if lv["born"] > daily.index[-1]:
        continue
    t0 = pd.Timestamp(lv["born"]) - pd.Timedelta(hours=26)
    t1 = df.index[-1]
    col = "#d62728" if lv["kind"] == "res" else "#2ca02c"
    active = not (lv["used"] or lv["dead"])
    ax[0].hlines(lv["price"], t0, t1, color=col, ls="-" if active else ":",
                 lw=1.3 if active else 1.0, alpha=0.85 if active else 0.45, zorder=2)
    tag = "阻" if lv["kind"] == "res" else "支"
    ax[0].annotate("{} {:.0f}".format(tag, lv["price"]), (t0, lv["price"]),
                   fontsize=7, color=col, xytext=(2, 4),
                   textcoords="offset points", ha="left", alpha=0.9)

# 交易: 持仓区间 + 入场/出场标记 + 止损止盈线 + 盈亏标注(交错偏移防重叠)
for ti, t in enumerate(trades):
    c = "#e60000" if t["side"] == "long" else "#008a00"
    ax[0].axvspan(t["t_in"], t["t_out"], color=c, alpha=0.05, zorder=1)
    ax[0].plot([t["t_in"], t["t_out"]], [t["entry"], t["exit"]],
               color=c, ls="--", lw=0.9, alpha=0.55, zorder=4)
    ax[0].hlines(t["sl"], t["t_in"], t["t_out"], color="#c00", ls="-",
                 lw=1.0, alpha=0.6, zorder=4)
    ax[0].hlines(t["tp"], t["t_in"], t["t_out"], color="#090", ls="-",
                 lw=1.0, alpha=0.6, zorder=4)
    m = "^" if t["side"] == "long" else "v"
    ax[0].scatter(t["t_in"], t["entry"], marker=m, color=c, s=110, zorder=6,
                  edgecolors="white", linewidths=0.8)
    dy = (14 if t["side"] == "long" else -18) * (1 + (ti % 3) * 0.55)
    ax[0].annotate("开{} {:.0f}\n止损{:.1f}%".format(
        "多" if t["side"] == "long" else "空", t["entry"], t["sl_pct"] * 100),
        (t["t_in"], t["entry"]), fontsize=6.8, color=c,
        xytext=(0, dy), textcoords="offset points", ha="center")
    ax[0].scatter(t["t_out"], t["exit"], marker="x", color=c, s=75, zorder=6)
    win = t["pnl"] > 0
    ax[0].annotate("{} {:+.0f}U".format(t["kind"], t["pnl"]),
                   (t["t_out"], t["exit"]), fontsize=7.5,
                   color="#c00" if win else "#060",
                   xytext=(8, (10 if win else -14) * (1 + (ti % 2) * 0.8)),
                   textcoords="offset points")

# 阶段背景说明
ax[0].set_ylabel("价格 (USDT)")
ax[0].grid(alpha=0.2)
ax[0].legend(loc="upper left", fontsize=8)
ax[0].set_title("黄金关键位反转策略 - 逐笔详细标注 | 红线=止损 绿线=止盈 "
                "| 色带=持仓区间 | 实线关键位=活跃 虚线=已失效",
                fontsize=11)

# 副图: 权益 + 每笔盈亏标记 + 最大回撤区间
ax[1].plot(eq.index, eq.values, color="tab:blue", lw=1.1, label="已实现权益")
ax[1].axhline(INIT_BAL, ls="--", c="gray", lw=0.7)
cm = eq.cummax()
ddser = eq / cm - 1
trough_t = ddser.idxmin()
peak_t = eq.loc[:trough_t].idxmax()
ax[1].axvspan(peak_t, trough_t, color="orange", alpha=0.18, zorder=1)
ax[1].annotate("最大回撤 {:.1f}%".format(dd * 100),
               (peak_t + (trough_t - peak_t) / 2, eq.loc[peak_t]),
               fontsize=8, color="#b60", ha="center",
               xytext=(0, 10), textcoords="offset points")
for t in trades:
    c = "#c00" if t["pnl"] > 0 else "#060"
    ax[1].scatter(t["t_out"], t["bal"], color=c, s=42, zorder=5,
                  edgecolors="white", linewidths=0.6)
    ax[1].annotate("{:+.0f}".format(t["pnl"]), (t["t_out"], t["bal"]),
                   fontsize=6.5, color=c, xytext=(0, 7 if t["pnl"] > 0 else -11),
                   textcoords="offset points", ha="center")
ax[1].set_ylabel("权益 (U)")
ax[1].grid(alpha=0.2)
ax[1].legend(loc="upper left", fontsize=8)
PNG = "gold_swing_backtest_result_{}_rr{:g}.png".format(TF, RR)
plt.tight_layout()
plt.savefig(PNG, dpi=140)
print("\n图: " + PNG)
