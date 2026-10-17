# -*- coding: utf-8 -*-
"""
BTC/ETH 现货行情 MACD 二次交叉形态 全仓杠杆模拟回测（参数化通用版）

用法（默认值即历史验证过的 1h/1%/2% 最优组合）:
    python macd_backtest.py
    python macd_backtest.py --timeframe 4h --sl 0.03 --tp 0.03
    python macd_backtest.py --timeframe 1d --sl 0.03 --tp 0.06 --days 365
    python macd_backtest.py --symbols BTCUSDT --days 180

策略（与历史各版本严格一致）:
    做多: MACD 零轴水下 金叉1 -> 死叉 -> 金叉2，且金叉2 的 DIF 值高于金叉1
    做空: MACD 零轴水上 死叉1 -> 金叉 -> 死叉2，且死叉2 的 DIF 值低于死叉1
    DIF 越过零轴则形态状态机重置
资金: 每币独立 1000U，固定 100U 保证金，100x 杠杆，名义 10000U
模式: 全仓（强平价按账户权益动态计算），止损/止盈见参数
成本: 手续费 0.04%/边（名义价值），资金费 0.01%/8h 按周期折算（多付空收）

出场顺序（已修复的关键口径，勿改回固定顺序）:
    每根K线比较止损价与强平价哪个离入场价更近，连续走势先触更近者；
    开盘跳空越过止损按开盘价滑点成交，跳过强平价直接强平；
    同根K线同时触止损与止盈时保守按止损。
"""
import argparse
import math
import time

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests

API_URL = "https://data-api.binance.vision/api/v3/klines"
INIT_BALANCE = 1000.0
MARGIN = 100.0
LEVERAGE = 100
NOTIONAL = MARGIN * LEVERAGE
MAINT_RATE = 0.005
FEE_RATE = 0.0004
FUND_8H = 0.0001  # 资金费 0.01%/8h

# 周期 -> (每年K线数, 每根K线包含的资金费结算次数)
TF_SPEC = {
    "15m": (4 * 24 * 365, 0.25 / 8),
    "1h":  (24 * 365, 1 / 8),
    "4h":  (6 * 365, 0.5),
    "1d":  (365, 3.0),
}
BAR_LABEL = {"15m": "15m", "1h": "1h", "4h": "4h", "1d": "个交易日"}

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Arial Unicode MS"]
plt.rcParams["axes.unicode_minus"] = False


def _fetch_page(symbol, interval, start_ms, limit=1000):
    r = requests.get(API_URL,
                     params={"symbol": symbol, "interval": interval,
                             "startTime": start_ms, "limit": limit},
                     timeout=15, headers={"User-Agent": "backtest/1.0"})
    r.raise_for_status()
    raw = r.json()
    if not isinstance(raw, list):
        raise RuntimeError("接口返回异常: {}".format(str(raw)[:120]))
    return raw


def fetch_klines(symbol, interval, days):
    """分页拉取最近 days 天 K线（Binance 单次上限 1000 根）。"""
    start_ms = int((time.time() - days * 86400) * 1000)
    all_raw, cursor = [], start_ms
    for _ in range(50):
        page = None
        for attempt in range(4):
            try:
                page = _fetch_page(symbol, interval, cursor)
                break
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(1.5 * (attempt + 1))
        if not page:
            break
        all_raw += page
        if len(page) < 1000:
            break
        cursor = int(page[-1][0]) + 1
        time.sleep(0.25)
    if not all_raw:
        raise RuntimeError("{} K线获取失败：返回为空".format(symbol))
    df = pd.DataFrame(all_raw, columns=[
        "ts", "open", "high", "low", "close", "volume",
        "close_ts", "qav", "trades", "tbbav", "tbqav", "ignore"])
    df["time"] = pd.to_datetime(df["ts"].astype("int64"), unit="ms", utc=True)
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = df[c].astype("float64")
    return (df.drop_duplicates("ts").sort_values("ts")
              .set_index("time")[["open", "high", "low", "close", "volume"]])


def add_macd(df, fast=12, slow=26, signal=9):
    close = df["close"]
    df["dif"] = close.ewm(span=fast, adjust=False).mean() - close.ewm(span=slow, adjust=False).mean()
    df["dea"] = df["dif"].ewm(span=signal, adjust=False).mean()
    df["hist"] = 2 * (df["dif"] - df["dea"])
    return df


def generate_signals(df):
    """
    双状态机并行：
      多头(水下): 金叉1(DIF<0) -> 死叉 -> 金叉2 且 DIF2>DIF1  => signal=+1
      空头(水上): 死叉1(DIF>0) -> 金叉 -> 死叉2 且 DIF2<DIF1  => signal=-1
    DIF 一旦越过零轴，对应状态机重置，保证形态在同一侧完成。
    注意：金叉2 无论是否满足 DIF2>DIF1，都成为新一轮金叉1（信号可连续触发）。
    """
    dif, dea = df["dif"].values, df["dea"].values
    n = len(df)
    signals = np.zeros(n, dtype=np.int8)

    # 多头状态机: 0=等金叉1, 1=等死叉, 2=等金叉2
    l_state, l_first = 0, np.nan
    # 空头状态机: 0=等死叉1, 1=等金叉, 2=等死叉2
    s_state, s_first = 0, np.nan

    for i in range(1, n):
        if np.isnan(dif[i]) or np.isnan(dea[i]):
            continue
        golden = dif[i - 1] <= dea[i - 1] and dif[i] > dea[i]
        death = dif[i - 1] >= dea[i - 1] and dif[i] < dea[i]

        # --- 多头（水下）---
        if dif[i] >= 0:
            l_state, l_first = 0, np.nan          # 离开零轴下方，形态作废
        elif golden:
            if l_state == 0:
                l_state, l_first = 1, dif[i]
            elif l_state == 2:
                if dif[i] > l_first:
                    signals[i] = 1
                l_state, l_first = 1, dif[i]      # 以本次金叉作为新一轮起点
        elif death and l_state == 1:
            l_state = 2

        # --- 空头（水上）---
        if dif[i] <= 0:
            s_state, s_first = 0, np.nan          # 离开零轴上方，形态作废
        elif death:
            if s_state == 0:
                s_state, s_first = 1, dif[i]
            elif s_state == 2:
                if dif[i] < s_first:
                    signals[i] = -1
                s_state, s_first = 1, dif[i]
        elif golden and s_state == 1:
            s_state = 2

    df["signal"] = signals
    return df


def apply_trend_filter(df, period=200):
    """趋势过滤：收盘价 > EMA(period) 只保留多头信号，< EMA 只保留空头信号。
    过滤掉在主趋势反向一侧触发的逆势形态信号（如上涨趋势中的做空形态）。"""
    df = df.copy()
    df["ema_trend"] = df["close"].ewm(span=period, adjust=False).mean()
    above = df["close"] > df["ema_trend"]
    df.loc[above & (df["signal"] < 0), "signal"] = 0   # 上升趋势禁做空信号
    df.loc[~above & (df["signal"] > 0), "signal"] = 0  # 下降趋势禁做多信号
    return df


def run_backtest(df, name, sl_pct, tp_pct, fund_per_bar):
    """资金口径与历史版本严格一致：
    开仓不动 balance（全仓，费用全部计入平仓 pnl）；
    平仓 pnl = 名义价值*价格变动% - 资金费(带方向) - 双边手续费；
    强平 pnl = -(入场时权益 - 名义价值*维持保证金率)；
    期末仍有持仓按最后收盘价平仓。"""
    balance = INIT_BALANCE
    pos = None
    trades = []
    equity = np.empty(len(df))

    def close_trade(i, exit_price, reason):
        nonlocal balance, pos
        side = pos["side"]
        entry = pos["entry"]
        chg = (exit_price - entry) / entry if side == 1 else (entry - exit_price) / entry
        if reason == "强平":
            pnl = -(pos["entry_balance"] - NOTIONAL * MAINT_RATE)
        else:
            pnl = NOTIONAL * chg
        pnl -= pos["fund"]                            # 资金费（多付空收，已带方向）
        pnl -= pos["entry_fee"] + NOTIONAL * FEE_RATE  # 开仓+平仓手续费
        balance += pnl
        trades.append({
            "side": side, "entry_time": pos["time"], "exit_time": df.index[i],
            "entry": entry, "exit": exit_price, "reason": reason,
            "pnl": pnl, "fund": pos["fund"],
            "bars": i - pos["idx"], "balance": balance,
        })
        pos = None

    for i in range(len(df)):
        row = df.iloc[i]
        opn, high, low, close = row["open"], row["high"], row["low"], row["close"]

        # 出场判定：止损与强平谁离入场价更近先触谁；开盘跳空越过止损按开盘价
        # 滑点成交、跳过强平直接强平；同根K线触止损与止盈按止损。
        if pos is not None and i > pos["idx"]:
            side = pos["side"]
            if side == 1:
                pos["fund"] += NOTIONAL * fund_per_bar   # 多头付资金费
                if opn <= pos["liq"]:
                    close_trade(i, pos["liq"], "强平")
                elif opn < pos["sl"]:
                    close_trade(i, opn, "止损")
                elif pos["liq"] > pos["sl"] and low <= pos["liq"]:
                    close_trade(i, pos["liq"], "强平")   # 权益不足，强平比止损更近
                elif low <= pos["sl"]:
                    close_trade(i, pos["sl"], "止损")
                elif high >= pos["tp"]:
                    close_trade(i, pos["tp"], "止盈")
            else:
                pos["fund"] -= NOTIONAL * fund_per_bar   # 空头收资金费
                if opn >= pos["liq"]:
                    close_trade(i, pos["liq"], "强平")
                elif opn > pos["sl"]:
                    close_trade(i, opn, "止损")
                elif pos["liq"] < pos["sl"] and high >= pos["liq"]:
                    close_trade(i, pos["liq"], "强平")
                elif high >= pos["sl"]:
                    close_trade(i, pos["sl"], "止损")
                elif low <= pos["tp"]:
                    close_trade(i, pos["tp"], "止盈")

        # 无持仓且出现信号：当根收盘价开仓（费用计入平仓 pnl，此处仅风控预留校验）
        if pos is None and row["signal"] != 0 and balance >= MARGIN + NOTIONAL * FEE_RATE:
            entry = close
            liq_dist = max((balance - NOTIONAL * MAINT_RATE) / NOTIONAL, 0)
            common = {"entry": entry, "idx": i, "time": df.index[i],
                      "entry_balance": balance, "fund": 0.0,
                      "entry_fee": NOTIONAL * FEE_RATE}
            if row["signal"] == 1:
                pos = dict(common, side=1,
                           liq=entry * (1 - liq_dist),
                           sl=entry * (1 - sl_pct), tp=entry * (1 + tp_pct))
            else:
                pos = dict(common, side=-1,
                           liq=entry * (1 + liq_dist),
                           sl=entry * (1 + sl_pct), tp=entry * (1 - tp_pct))

        # 盯市权益（浮动盈亏，并预留双边手续费）
        if pos is not None:
            chg = (close - pos["entry"]) / pos["entry"] * pos["side"]
            equity[i] = balance + NOTIONAL * chg - pos["entry_fee"] - NOTIONAL * FEE_RATE
        else:
            equity[i] = balance

    if pos is not None:
        close_trade(len(df) - 1, df["close"].iloc[-1], "期末平仓")

    df = df.copy()
    df["equity"] = equity
    return df, trades


def calc_metrics(df, trades, bars_per_year):
    eq = df["equity"]
    total_ret = eq.iloc[-1] / INIT_BALANCE - 1
    peak = eq.cummax()
    max_dd = ((eq - peak) / peak).min()
    rets = eq.pct_change().dropna()
    sharpe = float(rets.mean() / rets.std() * np.sqrt(bars_per_year)) if len(rets) > 1 and rets.std() > 0 else 0.0
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    win_rate = len(wins) / len(trades) if trades else 0.0
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    profit_factor = gross_win / gross_loss if gross_loss > 0 else math.inf
    avg_win = gross_win / len(wins) if wins else 0.0
    avg_loss = gross_loss / len(losses) if losses else 0.0
    pl_ratio = avg_win / avg_loss if avg_loss > 0 else math.inf
    avg_bars = np.mean([t["bars"] for t in trades]) if trades else 0.0
    n_l = sum(1 for t in trades if t["side"] == 1)
    n_s = sum(1 for t in trades if t["side"] == -1)
    n_tp = sum(1 for t in trades if t["reason"] == "止盈")
    n_sl = sum(1 for t in trades if t["reason"] == "止损")
    n_liq = sum(1 for t in trades if t["reason"] == "强平")
    return {
        "交易数": len(trades), "多": n_l, "空": n_s, "止盈": n_tp, "止损": n_sl, "强平": n_liq,
        "期末权益": eq.iloc[-1], "总收益率": total_ret, "胜率": win_rate,
        "利润因子": profit_factor, "盈亏比": pl_ratio, "最大回撤": max_dd,
        "夏普": sharpe, "平均持仓": avg_bars,
    }


def fmt(x, nd=2):
    if isinstance(x, float) and math.isinf(x):
        return "inf"
    return round(x, nd) if isinstance(x, float) else x


def save_report(results, args, path):
    L = []
    L.append("=" * 72)
    L.append("{} 现货行情 MACD 二次交叉形态 多空回测报告（{}/{}天/100x杠杆模拟）".format(
        "、".join(args.symbols), args.timeframe, args.days))
    L.append("多头: 水下金叉1->死叉->金叉2(DIF2更高)  空头: 水上死叉1->金叉->死叉2(DIF2更低)")
    L.append("本金1000U/币 | 固定100U保证金 | 100x杠杆 | 全仓 | 止损{:.0%} | 止盈{:.0%}(1:{:.0f})".format(
        args.sl, args.tp, args.tp / args.sl))
    L.append("手续费0.04%/边 | 资金费0.01%/8h(多付空收) | 数据: Binance现货{}K线".format(args.timeframe))
    if args.trend_filter:
        L.append("趋势过滤: EMA{}（价格在EMA上方只做多、下方只做空，逆势形态信号被跳过）".format(args.trend_filter))
    L.append("=" * 72)
    total_end = 0.0
    for name, (df, trades, m) in results.items():
        total_end += m["期末权益"]
        L.append("")
        L.append("【{}】".format(name))
        L.append("  交易: {}笔(多{}/空{})  止盈{} 止损{} 强平{}".format(
            m["交易数"], m["多"], m["空"], m["止盈"], m["止损"], m["强平"]))
        L.append("  期末权益: {:.2f}U  收益率: {:+.2%}".format(m["期末权益"], m["总收益率"]))
        L.append("  胜率: {:.1%}  利润因子: {}  盈亏比: {}".format(m["胜率"], fmt(m["利润因子"]), fmt(m["盈亏比"])))
        L.append("  最大回撤: {:.2%}  夏普: {}  平均持仓: {:.1f} 根{} K线".format(
            m["最大回撤"], fmt(m["夏普"]), m["平均持仓"], BAR_LABEL.get(args.timeframe, args.timeframe)))
        for k, t in enumerate(trades, 1):
            L.append("    #{:<2d}{} {} -> {}  {:.2f}->{:.2f}  {}  pnl={:+.2f}U  余额={:.2f}U".format(
                k, "多" if t["side"] == 1 else "空",
                t["entry_time"].strftime("%m-%d %H:%M"), t["exit_time"].strftime("%m-%d %H:%M"),
                t["entry"], t["exit"], t["reason"], t["pnl"], t["balance"]))
    L.append("")
    n = len(results)
    L.append("组合({}个币种,各1000U,共{:.0f}U)期末: {:.2f}U  组合收益: {:+.2%}".format(
        n, INIT_BALANCE * n, total_end, total_end / (INIT_BALANCE * n) - 1))
    text = "\n".join(L)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    print("\n报告已保存: {}".format(path))


def plot_results(results, args, path):
    n = len(results)
    fig, axes = plt.subplots(n, 4, figsize=(24, 5.2 * n))
    if n == 1:
        axes = [axes]
    for r, (name, (df, trades, m)) in enumerate(results.items()):
        ax = axes[r][0]
        ax.plot(df.index, df["close"], lw=0.8, color="#333")
        for t in trades:
            c = "#d62728" if t["side"] == 1 else "#1f77b4"
            mk = "^" if t["side"] == 1 else "v"
            ax.scatter(t["entry_time"], t["entry"], marker=mk, color=c, s=60, zorder=5)
            ax.scatter(t["exit_time"], t["exit"], marker="x", color=c, s=50, zorder=5)
        ax.set_title("{} {} 二次交叉形态回测 100x全仓 {:.0%}/{:.0%}(1:{:.0f}) {}天 收益{:+.1%}".format(
            name, args.timeframe, args.sl, args.tp, args.tp / args.sl, args.days, m["总收益率"]))
        ax.grid(alpha=0.3)

        ax = axes[r][1]
        ax.plot(df.index, df["dif"], lw=0.7, label="DIF")
        ax.plot(df.index, df["dea"], lw=0.7, label="DEA")
        ax.axhline(0, color="#999", lw=0.6)
        ax.legend(fontsize=8)
        ax.set_title("MACD(12,26,9)")
        ax.grid(alpha=0.3)

        ax = axes[r][2]
        ax.plot(df.index, df["equity"], lw=1.0, color="#2ca02c")
        ax.axhline(INIT_BALANCE, color="#999", ls="--", lw=0.8)
        ax.set_title("权益曲线  期末 {:.0f}U".format(m["期末权益"]))
        ax.grid(alpha=0.3)

        ax = axes[r][3]
        peak = df["equity"].cummax()
        dd = (df["equity"] - peak) / peak
        ax.fill_between(dd.index, dd * 100, 0, color="#d62728", alpha=0.4)
        ax.set_title("回撤  最大 {:.1%}".format(m["最大回撤"]))
        ax.grid(alpha=0.3)

    plt.tight_layout()
    plt.savefig(path, dpi=90)
    plt.close()
    print("图表已保存: {}".format(path))


def main():
    ap = argparse.ArgumentParser(description="BTC/ETH MACD二次交叉形态 全仓杠杆模拟回测")
    ap.add_argument("--timeframe", default="1h", choices=sorted(TF_SPEC), help="K线周期")
    ap.add_argument("--days", type=int, default=90, help="回看天数")
    ap.add_argument("--sl", type=float, default=0.01, help="止损比例")
    ap.add_argument("--tp", type=float, default=0.02, help="止盈比例")
    ap.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT"], help="交易对")
    ap.add_argument("--trend-filter", type=int, default=0,
                    help="趋势过滤EMA周期（0=关闭，如200）")
    ap.add_argument("--tag", default="", help="输出文件名附加标记")
    args = ap.parse_args()

    bars_per_year = TF_SPEC[args.timeframe][0]
    # 每根K线的资金费结算次数: 15m=1/32, 1h=1/8, 4h=1/2, 1d=3
    fund_per_bar = FUND_8H * {"15m": 1 / 32, "1h": 1 / 8, "4h": 1 / 2, "1d": 3.0}[args.timeframe]

    print("行情: Binance现货{} K线 | 本金1000U | 固定100U | 100x全仓 | 止损{:.0%} 止盈{:.0%} | 近{}天".format(
        args.timeframe, args.sl, args.tp, args.days))

    results = {}
    for sym in args.symbols:
        print("拉取 {} {} K线 ...".format(sym, args.timeframe))
        df = generate_signals(add_macd(fetch_klines(sym, args.timeframe, args.days)))
        if args.trend_filter:
            df = apply_trend_filter(df, args.trend_filter)
        df, trades = run_backtest(df, sym, args.sl, args.tp, fund_per_bar)
        results[sym] = (df, trades, calc_metrics(df, trades, bars_per_year))

    tag = args.tag or "{}_sl{:.0f}pct_tp{:.0f}pct_{}d".format(
        args.timeframe, args.sl * 100, args.tp * 100, args.days)
    save_report(results, args, "macd_backtest_{}_report.txt".format(tag))
    plot_results(results, args, "macd_backtest_{}_result.png".format(tag))


if __name__ == "__main__":
    main()
