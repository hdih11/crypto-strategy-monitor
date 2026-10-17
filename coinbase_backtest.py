# -*- coding: utf-8 -*-
"""
基于 Coinbase Exchange API 的 BTC/ETH 合约策略回测
策略: 4h MACD 金叉->死叉->金叉 做多 (只做多)
本金 1000U, 每次固定 100U 保证金, 100倍杠杆, 全仓模式, 止损 3%, 盈亏比 1:1 (止盈 3%)
注意: 全仓模式下全部余额作为担保, 强平线 = (余额-维持保证金)/名义价值, 通常远于3%止损
"""
import time
import requests
import pandas as pd

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Arial Unicode MS"]
    plt.rcParams["axes.unicode_minus"] = False
    HAVE_MPL = True
except Exception:
    HAVE_MPL = False

# ---------- 参数 ----------
SYMBOLS = ["BTC-USD", "ETH-USD"]
GRANULARITY = 3600           # Coinbase 不支持4h, 拉1h再聚合
RESAMPLE = "4h"
DAYS = 90
INIT_BALANCE = 1000.0
MARGIN_PER_TRADE = 100.0     # 每次固定开仓保证金
LEVERAGE = 100
NOTIONAL = MARGIN_PER_TRADE * LEVERAGE   # 10000U
SL_PCT = 0.03                # 止损 3%
TP_PCT = 0.03                # 止盈 3% (盈亏比1:1)
LIQ_PCT = 0.01               # (逐仓参数, 全仓模式下不使用)
MAINT_RATE = 0.005           # 维持保证金率 0.5% (全仓强平判定)
FEE_RATE = 0.0004            # 手续费 0.04%/边 (按名义价值)
FUNDING_RATE = 0.0001        # 资金费率 0.01%/8h -> 每4h收一半
FUNDING_PER_CANDLE = FUNDING_RATE / 2 * NOTIONAL

BASE_URL = "https://api.exchange.coinbase.com/products/{}/candles"
PROXIES = {"http": "http://127.0.0.1:9674", "https": "http://127.0.0.1:9674"}


def fetch_candles(symbol):
    """分页拉取最近 DAYS 天的 4h K线 (Coinbase 单次最多300根)"""
    end = int(time.time())
    start = end - DAYS * 86400
    all_rows = []
    cur_start = start
    while cur_start < end:
        cur_end = min(cur_start + 299 * GRANULARITY, end)
        for attempt in range(5):  # 代理偶发断连, 重试
            try:
                r = requests.get(
                    BASE_URL.format(symbol),
                    params={"start": cur_start, "end": cur_end, "granularity": GRANULARITY},
                    headers={"User-Agent": "backtest/1.0"},
                    proxies=PROXIES,
                    timeout=15,
                )
                r.raise_for_status()
                data = r.json()
                break
            except Exception:
                if attempt == 4:
                    raise
                time.sleep(1 + attempt)
        if not isinstance(data, list):
            raise RuntimeError(f"{symbol} API返回异常: {data}")
        all_rows.extend(data)
        cur_start = cur_end
        time.sleep(0.3)
    # Coinbase 返回 [time, low, high, open, close, volume], 倒序
    df = pd.DataFrame(all_rows, columns=["ts", "low", "high", "open", "close", "vol"])
    df = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    df["time"] = pd.to_datetime(df["ts"], unit="s")
    # 1h 聚合为 4h
    df = df.set_index("time")
    df = df.resample(RESAMPLE).agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "vol": "sum"
    }).dropna().reset_index()
    return df[["time", "open", "high", "low", "close", "vol"]]


def add_macd(df, fast=12, slow=26, signal=9):
    ema_fast = df["close"].ewm(span=fast, adjust=False).mean()
    ema_slow = df["close"].ewm(span=slow, adjust=False).mean()
    df["dif"] = ema_fast - ema_slow
    df["dea"] = df["dif"].ewm(span=signal, adjust=False).mean()
    return df


def detect_crosses(df):
    """返回每根K线的交叉标记: 1=金叉, -1=死叉, 0=无"""
    diff = df["dif"] - df["dea"]
    cross = [0] * len(df)
    for i in range(1, len(df)):
        if diff.iloc[i - 1] <= 0 and diff.iloc[i] > 0:
            cross[i] = 1
        elif diff.iloc[i - 1] >= 0 and diff.iloc[i] < 0:
            cross[i] = -1
    return cross


def run_backtest(df, symbol):
    df = add_macd(df)
    crosses = detect_crosses(df)
    balance = INIT_BALANCE
    position = None          # dict: side, entry, entry_idx
    recent_crosses = []      # 最近的交叉序列
    trades = []
    equity_curve = []

    for i in range(len(df)):
        row = df.iloc[i]

        # ---------- 持仓中: 检查强平/止损/止盈 ----------
        if position is not None and i > position["entry_idx"]:
            side = position["side"]
            entry = position["entry"]
            # 全仓: 强平距离 = (开仓时余额 - 维持保证金) / 名义价值
            maint = NOTIONAL * MAINT_RATE
            liq_pct = max((position["entry_balance"] - maint) / NOTIONAL, 0)
            if side == "long":
                liq_p = entry * (1 - liq_pct)
                sl_p = entry * (1 - SL_PCT)
                tp_p = entry * (1 + TP_PCT)
                if row["low"] <= liq_p:
                    exit_type, exit_p = "强平", liq_p
                elif row["low"] <= sl_p:
                    exit_type, exit_p = "止损", sl_p
                elif row["high"] >= tp_p:
                    exit_type, exit_p = "止盈", tp_p
                else:
                    exit_type = None
            else:
                liq_p = entry * (1 + liq_pct)
                sl_p = entry * (1 + SL_PCT)
                tp_p = entry * (1 - TP_PCT)
                if row["high"] >= liq_p:
                    exit_type, exit_p = "强平", liq_p
                elif row["high"] >= sl_p:
                    exit_type, exit_p = "止损", sl_p
                elif row["low"] <= tp_p:
                    exit_type, exit_p = "止盈", tp_p
                else:
                    exit_type = None

            # 资金费: 每持有一根4h收一次
            position["funding"] += FUNDING_PER_CANDLE

            if exit_type:
                price_chg = (exit_p - entry) / entry if side == "long" else (entry - exit_p) / entry
                if exit_type == "强平":
                    pnl = -(position["entry_balance"] - maint)  # 全仓强平: 亏到只剩维持保证金
                else:
                    pnl = NOTIONAL * price_chg
                pnl -= position["funding"]
                pnl -= NOTIONAL * FEE_RATE * 2  # 双边手续费
                balance += pnl
                trades.append({
                    "symbol": symbol,
                    "side": "做多" if side == "long" else "做空",
                    "entry_time": position["entry_time"],
                    "exit_time": row["time"],
                    "entry": round(entry, 2),
                    "exit": round(exit_p, 2),
                    "exit_type": exit_type,
                    "pnl": round(pnl, 2),
                    "balance": round(balance, 2),
                })
                position = None

        # ---------- 无持仓: 检测交叉形态 ----------
        if crosses[i] != 0:
            recent_crosses.append(crosses[i])
            if len(recent_crosses) > 3:
                recent_crosses = recent_crosses[-3:]
            if position is None and len(recent_crosses) == 3:
                if recent_crosses == [1, -1, 1]:      # 金叉-死叉-金叉 -> 做多
                    side = "long"
                else:
                    side = None
                # 余额不足保证金时不再开仓
                if side and balance >= MARGIN_PER_TRADE:
                    position = {
                        "side": side,
                        "entry": row["close"],
                        "entry_idx": i,
                        "entry_time": row["time"],
                        "entry_balance": balance,  # 全仓: 记录开仓时余额用于强平判定
                        "funding": 0.0,
                    }

        equity_curve.append(balance)

    # ---------- 指标统计 ----------
    df["equity"] = equity_curve
    n = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    total_ret = (balance - INIT_BALANCE) / INIT_BALANCE * 100

    peak = -1e18
    max_dd = 0.0
    for eq in equity_curve:
        peak = max(peak, eq)
        if peak > 0:
            max_dd = max(max_dd, (peak - eq) / peak * 100)

    eq_s = pd.Series(equity_curve)
    rets = eq_s.pct_change().dropna()
    sharpe = (rets.mean() / rets.std() * (6 * 365) ** 0.5) if rets.std() > 0 else 0.0

    return {
        "symbol": symbol,
        "df": df,
        "trades": trades,
        "balance": balance,
        "total_ret": total_ret,
        "n_trades": n,
        "win_rate": len(wins) / n * 100 if n else 0,
        "profit_factor": gross_win / gross_loss if gross_loss > 0 else float("inf"),
        "max_dd": max_dd,
        "sharpe": sharpe,
        "liq_count": sum(1 for t in trades if t["exit_type"] == "强平"),
        "sl_count": sum(1 for t in trades if t["exit_type"] == "止损"),
        "tp_count": sum(1 for t in trades if t["exit_type"] == "止盈"),
    }


def plot_results(results, out_path="backtest_btc_eth_result.png"):
    """左列BTC右列ETH：价格+开平仓信号 / 净值曲线 / 回撤曲线"""
    fig, axes = plt.subplots(3, 2, figsize=(16, 11), sharex="col",
                             gridspec_kw={"height_ratios": [3, 1.2, 1.2]})
    for col, r in enumerate(results):
        df = r["df"]
        # 1) 价格 + 信号
        ax = axes[0][col]
        ax.plot(df["time"], df["close"], color="#333333", lw=1, label="收盘价")
        for t in r["trades"]:
            is_long = t["side"] == "做多"
            ax.scatter(t["entry_time"], t["entry"],
                       marker="^" if is_long else "v",
                       color="red" if is_long else "green", s=40, zorder=5)
            ax.scatter(t["exit_time"], t["exit"], marker="x", color="#888888", s=25, zorder=5)
        ax.scatter([], [], marker="^", color="red", label="做多开仓")
        ax.scatter([], [], marker="v", color="green", label="做空开仓")
        ax.set_ylabel("价格 (USD)")
        ax.legend(loc="upper left", fontsize=8)
        ax.set_title("{}  MACD交叉形态策略回测 ({}x杠杆, {}天)".format(r["symbol"], LEVERAGE, DAYS))

        # 2) 净值
        ax = axes[1][col]
        ax.plot(df["time"], df["equity"], color="#1f77b4", lw=1.2)
        ax.axhline(INIT_BALANCE, color="gray", ls="--", lw=0.8)
        ax.set_ylabel("权益 (U)")

        # 3) 回撤
        ax = axes[2][col]
        eq = df["equity"]
        dd = (eq - eq.cummax()) / eq.cummax() * 100
        ax.fill_between(df["time"], dd, 0, color="#d62728", alpha=0.5)
        ax.set_ylabel("回撤 (%)")
        ax.set_xlabel("时间")

    plt.tight_layout()
    plt.savefig(out_path, dpi=120)
    print("图表已保存: {}".format(out_path))


def save_report(results, out_path="backtest_btc_eth_report.txt"):
    """保存指标汇总与完整交易明细到文本报告"""
    lines = []
    lines.append("=" * 70)
    lines.append("BTC/ETH 合约策略回测报告")
    lines.append("策略: 4h MACD 金叉->死叉->金叉做多 (只做多)")
    lines.append("本金1000U | 每次100U保证金 | 100x杠杆 | 全仓模式 | 止损3% | 止盈3%(盈亏比1:1)")
    lines.append("手续费0.04%/边 | 资金费率0.01%/8h | 数据: Coinbase 现货K线聚合4h | 周期90天")
    lines.append("=" * 70)
    for r in results:
        lines.append("")
        lines.append("【{}】".format(r["symbol"]))
        lines.append("  交易次数: {}  (止盈{} / 止损{} / 强平{})".format(
            r["n_trades"], r["tp_count"], r["sl_count"], r["liq_count"]))
        lines.append("  期末资金: {:.2f}U   总收益率: {:+.2f}%".format(r["balance"], r["total_ret"]))
        lines.append("  胜率: {:.1f}%   利润因子: {:.2f}".format(r["win_rate"], r["profit_factor"]))
        lines.append("  最大回撤: {:.2f}%   夏普比率: {:.2f}".format(r["max_dd"], r["sharpe"]))
        lines.append("  --- 交易明细 ---")
        if not r["trades"]:
            lines.append("  (无交易)")
        for t in r["trades"]:
            lines.append("  {} {} 开{} -> {} {}平{}  盈亏{:+.2f}U  余额{:.2f}U".format(
                t["entry_time"], t["side"], t["entry"],
                t["exit_time"], t["exit_type"], t["exit"], t["pnl"], t["balance"]))
    text = "\n".join(lines)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(text)
    print("报告已保存: {}".format(out_path))


def main():
    results = []
    for sym in SYMBOLS:
        print(f"拉取 {sym} 数据...")
        df = fetch_candles(sym)
        print(f"  共 {len(df)} 根4h K线, {df['time'].iloc[0]} ~ {df['time'].iloc[-1]}")
        results.append(run_backtest(df, sym))

    print("\n" + "=" * 70)
    print("回测结果汇总 (本金1000U | 每次100U保证金 | 100x杠杆 | 全仓模式 | 只做多 | 止损3% | 止盈3%)")
    print("=" * 70)
    for r in results:
        print(f"\n【{r['symbol']}】")
        print(f"  交易次数: {r['n_trades']}  (止盈{r['tp_count']} / 止损{r['sl_count']} / 强平{r['liq_count']})")
        print(f"  期末资金: {r['balance']:.2f}U   总收益率: {r['total_ret']:+.2f}%")
        print(f"  胜率: {r['win_rate']:.1f}%   利润因子: {r['profit_factor']:.2f}")
        print(f"  最大回撤: {r['max_dd']:.2f}%   夏普比率: {r['sharpe']:.2f}")

    print("\n" + "=" * 70)
    print("交易明细")
    print("=" * 70)
    for r in results:
        print(f"\n--- {r['symbol']} ---")
        if not r["trades"]:
            print("  (无交易)")
        for t in r["trades"]:
            print(f"  {t['entry_time']} {t['side']} 开{t['entry']} -> "
                  f"{t['exit_time']} {t['exit_type']}平{t['exit']}  "
                  f"盈亏{t['pnl']:+.2f}U  余额{t['balance']:.2f}U")

    save_report(results)
    if HAVE_MPL:
        plot_results(results)
    else:
        print("未安装 matplotlib，跳过绘图")


if __name__ == "__main__":
    main()
