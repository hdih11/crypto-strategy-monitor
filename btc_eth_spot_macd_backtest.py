# -*- coding: utf-8 -*-
"""
BTC / ETH 现货行情 MACD 多空双向策略回测（近三个月）
=====================================================
说明   : 纯现货无法做空、也没有杠杆/全仓强平；本回测使用【现货 4h K线】作为
         行情源，在其上模拟 100x 全仓杠杆的多空双向交易（若只做现货多头，
         忽略脚本中 side=-1 的做空交易即可）
数据源 : Binance 现货 K线接口 data-api.binance.vision/api/v3/klines
         (公共数据镜像，无需代理可直连)
策略   : 4h 周期 MACD(12,26,9)
         做多形态: 零轴水下 金叉1 -> 死叉 -> 金叉2(金叉2 的 DIF 值高于金叉1)
         做空形态: 零轴水上 死叉1 -> 金叉 -> 死叉2(死叉2 的 DIF 值低于死叉1)
         形态必须在 DIF 连续处于零轴同一侧期间完成，越过零轴则状态机重置
资金   : 本金 1000U，每次固定 100U 保证金，100 倍杠杆 -> 名义价值 10000U
模式   : 全仓（全部账户权益为仓位担保，强平线约 -9.5%，远于 3% 止损）
风控   : 止损 3%，止盈 6%（盈亏比 1:2）
成本   : 手续费 0.04%/边（按名义价值），资金费 0.01%/8h（每根 4h 计一半，多付空收）
输出   : 控制台汇总 + spot_backtest_btc_eth_rr1to2_report.txt + spot_backtest_btc_eth_rr1to2_result.png
"""

import time
import requests
import numpy as np
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

# ---------------- 参数 ----------------
SYMBOLS = {"BTCUSDT": "BTC", "ETHUSDT": "ETH"}
API_URL = "https://data-api.binance.vision/api/v3/klines"
TIMEFRAME = "4h"
DAYS = 90

INIT_BALANCE = 1000.0      # 每个币种独立账户本金 (U)
MARGIN = 100.0             # 每次固定开仓保证金 (U)
LEVERAGE = 100             # 杠杆倍数
NOTIONAL = MARGIN * LEVERAGE   # 名义价值 10000U
SL_PCT = 0.03              # 止损 3%
TP_PCT = 0.06              # 止盈 6%（盈亏比 1:2）
MAINT_RATE = 0.005         # 维持保证金率 0.5%（全仓强平判定）
FEE_RATE = 0.0004          # 单边手续费 0.04%
FUND_4H = 0.0001 / 2       # 资金费 0.01%/8h，折算每根 4h

MACD_FAST, MACD_SLOW, MACD_SIG = 12, 26, 9


# ---------------- 数据获取 ----------------
def fetch_klines(symbol):
    """拉取最近 DAYS 天的 4h K线（90天约540根，单次 limit=1000 即可取全）。"""
    start_ms = int((time.time() - DAYS * 86400) * 1000)
    params = {"symbol": symbol, "interval": TIMEFRAME,
              "startTime": start_ms, "limit": 1000}
    last_err = None
    for attempt in range(4):
        try:
            r = requests.get(API_URL, params=params, timeout=15,
                             headers={"User-Agent": "backtest/1.0"})
            r.raise_for_status()
            raw = r.json()
            if not isinstance(raw, list) or not raw:
                raise RuntimeError("接口返回为空: {}".format(str(raw)[:120]))
            df = pd.DataFrame(raw, columns=[
                "ts", "open", "high", "low", "close", "volume",
                "close_ts", "qav", "trades", "tbbav", "tbqav", "ignore"])
            df["time"] = pd.to_datetime(df["ts"].astype("int64"), unit="ms", utc=True)
            for c in ("open", "high", "low", "close", "volume"):
                df[c] = df[c].astype("float64")
            df = (df.drop_duplicates("ts")
                    .sort_values("ts")
                    .set_index("time")[["open", "high", "low", "close", "volume"]])
            return df
        except Exception as e:
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError("{} K线获取失败: {}".format(symbol, last_err))


# ---------------- 指标与信号 ----------------
def add_macd(df):
    ema_f = df["close"].ewm(span=MACD_FAST, adjust=False).mean()
    ema_s = df["close"].ewm(span=MACD_SLOW, adjust=False).mean()
    df["dif"] = ema_f - ema_s
    df["dea"] = df["dif"].ewm(span=MACD_SIG, adjust=False).mean()
    return df


def generate_signals(df):
    """
    双状态机并行：
      多头(水下): 金叉1(DIF<0) -> 死叉 -> 金叉2 且 DIF2>DIF1  => signal=+1
      空头(水上): 死叉1(DIF>0) -> 金叉 -> 死叉2 且 DIF2<DIF1  => signal=-1
    DIF 一旦越过零轴，对应状态机重置，保证形态在同一侧完成。
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


# ---------------- 回测引擎（全仓，多空双向） ----------------
def run_backtest(df, name):
    balance = INIT_BALANCE
    pos = None           # {'side','entry','idx','time','entry_balance','liq','sl','tp','fund'}
    trades = []
    equity_curve = np.empty(len(df))

    def close_trade(i, exit_price, reason):
        nonlocal balance, pos
        side = pos["side"]
        entry = pos["entry"]
        chg = (exit_price - entry) / entry if side == 1 else (entry - exit_price) / entry
        if reason == "强平":
            pnl = -(pos["entry_balance"] - NOTIONAL * MAINT_RATE)
        else:
            pnl = NOTIONAL * chg
        pnl -= pos["fund"]                       # 资金费（已带多空方向）
        pnl -= pos["entry_fee"] + NOTIONAL * FEE_RATE   # 开仓+平仓手续费
        balance += pnl
        trades.append({
            "symbol": name,
            "side": "做多" if side == 1 else "做空",
            "entry_time": pos["time"], "entry": round(entry, 2),
            "exit_time": df.index[i], "exit": round(exit_price, 2),
            "reason": reason, "pnl": round(pnl, 2),
            "balance": round(balance, 2),
            "bars": i - pos["idx"],
            "ret_on_margin": round(pnl / MARGIN, 4),
        })
        pos = None

    for i in range(len(df)):
        row = df.iloc[i]
        opn, high, low, close = row["open"], row["high"], row["low"], row["close"]

        # 1) 持仓出场判定（按 K 线内真实可达顺序）：
        #    比较止损与强平哪个离入场价更近（全仓下账户权益缩水后强平价会近于止损），
        #    连续走势先触及更近者；开盘跳空越过时按开盘价滑点成交或直接强平。
        #    同一根K线同时触及止损与止盈时，保守按止损处理。
        if pos is not None and i > pos["idx"]:
            if pos["side"] == 1:
                pos["fund"] += NOTIONAL * FUND_4H       # 多头付资金费
                if opn <= pos["liq"]:
                    close_trade(i, pos["liq"], "强平")      # 开盘已越过强平价
                elif opn < pos["sl"]:
                    close_trade(i, opn, "止损")            # 开盘跳过止损：按开盘价成交
                elif pos["liq"] > pos["sl"] and low <= pos["liq"]:
                    close_trade(i, pos["liq"], "强平")      # 强平比止损更近(权益不足)
                elif low <= pos["sl"]:
                    close_trade(i, pos["sl"], "止损")
                elif high >= pos["tp"]:
                    close_trade(i, pos["tp"], "止盈")
            else:
                pos["fund"] -= NOTIONAL * FUND_4H       # 空头收资金费
                if opn >= pos["liq"]:
                    close_trade(i, pos["liq"], "强平")
                elif opn > pos["sl"]:
                    close_trade(i, opn, "止损")
                elif pos["liq"] < pos["sl"] and high >= pos["liq"]:
                    close_trade(i, pos["liq"], "强平")      # 强平比止损更近(权益不足)
                elif high >= pos["sl"]:
                    close_trade(i, pos["sl"], "止损")
                elif low <= pos["tp"]:
                    close_trade(i, pos["tp"], "止盈")

        # 2) 无持仓且出现信号：按当根收盘价开仓（手续费计入交易盈亏，此处仅做风控预留）
        if pos is None and row["signal"] != 0 and balance >= MARGIN + NOTIONAL * FEE_RATE:
            entry = close
            liq_dist = max((balance - NOTIONAL * MAINT_RATE) / NOTIONAL, 0)
            common = {"entry": entry, "idx": i, "time": df.index[i],
                      "entry_balance": balance, "fund": 0.0,
                      "entry_fee": NOTIONAL * FEE_RATE}
            if row["signal"] == 1:
                pos = dict(common, side=1,
                           liq=entry * (1 - liq_dist),
                           sl=entry * (1 - SL_PCT), tp=entry * (1 + TP_PCT))
            else:
                pos = dict(common, side=-1,
                           liq=entry * (1 + liq_dist),
                           sl=entry * (1 + SL_PCT), tp=entry * (1 - TP_PCT))

        # 3) 盯市权益（浮盈浮亏，并预留双边手续费）
        if pos is not None:
            chg = (close - pos["entry"]) / pos["entry"] * pos["side"]
            equity_curve[i] = balance + NOTIONAL * chg \
                - pos["entry_fee"] - NOTIONAL * FEE_RATE
        else:
            equity_curve[i] = balance

    if pos is not None:
        close_trade(len(df) - 1, df["close"].iloc[-1], "期末平仓")

    df = df.copy()
    df["equity"] = equity_curve
    return df, trades


# ---------------- 绩效统计 ----------------
def calc_metrics(df, trades):
    eq = df["equity"]
    final = float(eq.iloc[-1])
    total_ret = (final - INIT_BALANCE) / INIT_BALANCE
    ann_ret = (1 + total_ret) ** (365.0 / DAYS) - 1

    rets = eq.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    sharpe = float(rets.mean() / rets.std() * np.sqrt(6 * 365)) if rets.std() > 0 else 0.0

    dd = (eq - eq.cummax()) / eq.cummax()
    max_dd = float(dd.min())

    n = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gp = sum(t["pnl"] for t in wins)
    gl = abs(sum(t["pnl"] for t in losses))
    avg_w = np.mean([t["pnl"] for t in wins]) if wins else 0.0
    avg_l = abs(np.mean([t["pnl"] for t in losses])) if losses else 0.0

    return {
        "交易次数": n,
        "止盈/止损/强平": "{}/{}/{}".format(
            sum(t["reason"] == "止盈" for t in trades),
            sum(t["reason"] == "止损" for t in trades),
            sum(t["reason"] == "强平" for t in trades)),
        "多/空": "{}/{}".format(
            sum(t["side"] == "做多" for t in trades),
            sum(t["side"] == "做空" for t in trades)),
        "期末权益(U)": round(final, 2),
        "总收益率": total_ret,
        "年化收益率": ann_ret,
        "最大回撤": max_dd,
        "夏普比率": round(sharpe, 2),
        "胜率": len(wins) / n if n else 0.0,
        "利润因子": round(gp / gl, 2) if gl > 0 else np.inf,
        "盈亏比": round(avg_w / avg_l, 2) if avg_l > 0 else np.inf,
        "平均盈利(U)": round(avg_w, 2),
        "平均亏损(U)": round(avg_l, 2),
        "平均持仓K线": round(np.mean([t["bars"] for t in trades]), 1) if n else 0,
    }


# ---------------- 报告 ----------------
def save_report(results, path="spot_backtest_btc_eth_rr1to2_report.txt"):
    L = []
    L.append("=" * 72)
    L.append("BTC/ETH 现货行情 MACD 多空双向策略回测报告（近3个月 / 4h / 100x杠杆模拟）")
    L.append("多头: 水下金叉1->死叉->金叉2(DIF2更高)  空头: 水上死叉1->金叉->死叉2(DIF2更低)")
    L.append("本金1000U/币 | 固定100U保证金 | 100x杠杆 | 全仓 | 止损3% | 止盈6%(1:2)")
    L.append("手续费0.04%/边 | 资金费0.01%/8h(多付空收) | 数据: Binance现货4h K线")
    L.append("注: 纯现货无杠杆/做空，本报告为现货价格上的100x全仓多空模拟结果")
    L.append("=" * 72)
    for name, df, trades, m in results:
        L.append("")
        L.append("【{}】 K线区间: {} ~ {}  ({} 根)".format(
            name, df.index[0], df.index[-1], len(df)))
        L.append("  交易次数 {} (多{} / 空{})  出场: 止盈/止损/强平 = {}".format(
            m["交易次数"], m["多/空"].split("/")[0], m["多/空"].split("/")[1],
            m["止盈/止损/强平"]))
        L.append("  期末权益: {:.2f}U   总收益率: {:+.2%}   年化: {:+.2%}".format(
            m["期末权益(U)"], m["总收益率"], m["年化收益率"]))
        L.append("  胜率: {:.2%}   利润因子: {}   盈亏比: {}".format(
            m["胜率"], m["利润因子"], m["盈亏比"]))
        L.append("  最大回撤: {:.2%}   夏普: {}   平均持仓: {} 根4h K线".format(
            m["最大回撤"], m["夏普比率"], m["平均持仓K线"]))
        L.append("  平均盈利: {}U   平均亏损: {}U".format(
            m["平均盈利(U)"], m["平均亏损(U)"]))
        L.append("  --- 交易明细（UTC）---")
        if not trades:
            L.append("  (无交易)")
        for t in trades:
            L.append("  {} {} 开{:>9} -> {} {:>4}平{:>9}  持仓{:>3}根  盈亏{:>+8.2f}U  权益{:>8.2f}U".format(
                t["entry_time"].strftime("%m-%d %H:%M"), t["side"], t["entry"],
                t["exit_time"].strftime("%m-%d %H:%M"), t["reason"], t["exit"],
                t["bars"], t["pnl"], t["balance"]))

    comb_final = sum(m["期末权益(U)"] for _, _, _, m in results)
    L.append("")
    L.append("【组合】两币各投入1000U(合计2000U)，期末合计 {:.2f}U，组合收益率 {:+.2%}".format(
        comb_final, (comb_final - 2 * INIT_BALANCE) / (2 * INIT_BALANCE)))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("报告已保存: {}".format(path))


def plot_results(results, path="spot_backtest_btc_eth_rr1to2_result.png"):
    fig, axes = plt.subplots(4, 2, figsize=(16, 13), sharex="col",
                             gridspec_kw={"height_ratios": [3, 1.2, 1.2, 1.0]})
    for col, (name, df, trades, m) in enumerate(results):
        t_idx = df.index

        ax = axes[0][col]
        ax.plot(t_idx, df["close"], color="#333", lw=1, label="收盘价")
        for t in trades:
            up = t["side"] == "做多"
            ax.scatter(t["entry_time"], t["entry"], marker="^" if up else "v",
                       color="#d62728" if up else "#2ca02c", s=45, zorder=5)
            ax.scatter(t["exit_time"], t["exit"], marker="x",
                       color="#1f77b4" if t["reason"] == "止盈" else "#888", s=22, zorder=5)
        ax.scatter([], [], marker="^", color="#d62728", label="做多")
        ax.scatter([], [], marker="v", color="#2ca02c", label="做空")
        ax.set_ylabel("价格 (USDT)")
        ax.legend(loc="upper left", fontsize=8)
        ax.set_title("{} 现货K线 MACD水下/水上形态 多空回测  100x全仓 3%/6%(1:2) 90天  收益{:+.1%}".format(
            name, m["总收益率"]))

        ax = axes[1][col]
        ax.plot(t_idx, df["dif"], color="#ff7f0e", lw=0.9, label="DIF")
        ax.plot(t_idx, df["dea"], color="#1f77b4", lw=0.9, label="DEA")
        ax.axhline(0, color="gray", lw=0.7, ls="--")
        ax.set_ylabel("MACD")
        ax.legend(loc="upper left", fontsize=8)

        ax = axes[2][col]
        ax.plot(t_idx, df["equity"], color="#9467bd", lw=1.2)
        ax.axhline(INIT_BALANCE, color="gray", lw=0.8, ls="--")
        ax.set_ylabel("权益 (U)")

        ax = axes[3][col]
        dd = (df["equity"] - df["equity"].cummax()) / df["equity"].cummax() * 100
        ax.fill_between(t_idx, dd, 0, color="#d62728", alpha=0.45)
        ax.set_ylabel("回撤 (%)")
        ax.set_xlabel("UTC 时间")

    plt.tight_layout()
    plt.savefig(path, dpi=120)
    print("图表已保存: {}".format(path))


# ---------------- 主流程 ----------------
def main():
    print("=" * 72)
    print("BTC/ETH 现货行情 MACD 多空双向回测 | 水下金叉-死叉-金叉(DIF2更高)做多 / 水上死叉-金叉-死叉(DIF2更低)做空")
    print("行情: Binance现货4h K线 | 本金1000U | 固定100U | 100x杠杆模拟 | 全仓 | 止损3% | 止盈6%(1:2) | 近{}天".format(DAYS))
    print("=" * 72)

    results = []
    for symbol, name in SYMBOLS.items():
        print("拉取 {} 4h K线 ...".format(symbol))
        df = fetch_klines(symbol)
        print("  {} 根，{} ~ {}".format(len(df), df.index[0], df.index[-1]))
        df = add_macd(df)
        df = generate_signals(df)
        df, trades = run_backtest(df, name)
        m = calc_metrics(df, trades)
        results.append((name, df, trades, m))

    print("-" * 72)
    for name, df, trades, m in results:
        print("\n【{}】".format(name))
        print("  交易 {} 笔 (多{} 空{}) | 止盈/止损/强平 = {} | 平均持仓 {} 根".format(
            m["交易次数"], m["多/空"].split("/")[0], m["多/空"].split("/")[1],
            m["止盈/止损/强平"], m["平均持仓K线"]))
        print("  期末权益 {:.2f}U | 总收益 {:+.2%} | 年化 {:+.2%}".format(
            m["期末权益(U)"], m["总收益率"], m["年化收益率"]))
        print("  胜率 {:.2%} | 利润因子 {} | 盈亏比 {} | 最大回撤 {:+.2%} | 夏普 {}".format(
            m["胜率"], m["利润因子"], m["盈亏比"], m["最大回撤"], m["夏普比率"]))
        print("  平均盈利 {}U / 平均亏损 {}U".format(m["平均盈利(U)"], m["平均亏损(U)"]))
        print("  --- 明细(UTC) ---")
        if not trades:
            print("  (无交易)")
        for t in trades:
            print("  {} {} 开{} -> {} {}平{} 持{}根 盈亏{:+.2f}U 权益{:.2f}U".format(
                t["entry_time"].strftime("%m-%d %H:%M"), t["side"], t["entry"],
                t["exit_time"].strftime("%m-%d %H:%M"), t["reason"], t["exit"],
                t["bars"], t["pnl"], t["balance"]))

    comb = sum(m["期末权益(U)"] for _, _, _, m in results)
    print("\n【组合】合计投入2000U，期末 {:.2f}U，组合收益率 {:+.2%}".format(
        comb, (comb - 2000) / 2000))

    save_report(results)
    if HAVE_MPL:
        plot_results(results)
    else:
        print("未安装 matplotlib，跳过绘图")


if __name__ == "__main__":
    main()
