# -*- coding: utf-8 -*-
"""
BTC 永续合约交易策略回测
=========================
- 数据：Binance BTC/USDT 永续合约 4h K线（近三个月）；网络不可用时回退至合成数据
- 策略：4h MACD 水下金叉→死叉→金叉（第二个金叉高于第一个）做多
- 风控：止损 -1.5%，止盈 +4.5%（盈亏比 1:3），100x 杠杆，每笔 1% 本金
- 引擎：杠杆、仓位管理、强平判定、手续费、资金费率
- 指标：总收益、年化、胜率、盈亏比、利润因子、夏普、最大回撤
- 可视化：价格与买卖信号、净值曲线、回撤曲线（输出 PNG）
"""

import threading
import numpy as np
import pandas as pd

try:
    import matplotlib
    matplotlib.use("Agg")  # 无界面环境保存图片
    import matplotlib.pyplot as plt
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Arial Unicode MS"]
    plt.rcParams["axes.unicode_minus"] = False
    HAVE_MPL = True
except Exception:
    HAVE_MPL = False  # 未安装 matplotlib 时跳过绘图

try:
    import ccxt
    HAVE_CCXT = True
except Exception:
    HAVE_CCXT = False  # 未安装 ccxt 时回退合成数据

# ---------------- 全局参数 ----------------
SYMBOL = "BTC/USDT:USDT"   # OKX 永续合约交易对
TIMEFRAME = "4h"           # K线周期
DAYS = 90                  # 回测周期（天）
INIT_CASH = 1000.0         # 初始资金 (USDT)
LEVERAGE = 100             # 杠杆倍数
RISK_FRAC = 0.01           # 每次开仓保证金占权益比例（1% 本金）
FEE_RATE = 0.0004          # 单边手续费率 (0.04%)
FUNDING_RATE = 0.0001      # 每 8h 资金费率 (0.01%)
SWING_LOOKBACK = 20       # 阶段性低点回看根数（20 根 4h ≈ 3.3 天）
RR_RATIO = 3              # 盈亏比 1:3（止盈距离 = 3 × 止损距离）
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9


# ---------------- 数据获取 ----------------
def _fetch_okx():
    """从 OKX 永续合约拉取真实 4h K线。"""
    exchange = ccxt.okx({"enableRateLimit": True, "timeout": 10000})
    since = exchange.milliseconds() - DAYS * 24 * 60 * 60 * 1000
    all_ohlcv, from_ts = [], since
    while True:
        ohlcv = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, since=from_ts, limit=300)
        if not ohlcv:
            break
        all_ohlcv += ohlcv
        from_ts = ohlcv[-1][0] + 1
        if len(ohlcv) < 300:
            break
    df = pd.DataFrame(all_ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
    df["datetime"] = pd.to_datetime(df["ts"], unit="ms")
    df = df.drop_duplicates(subset="ts").set_index("datetime").drop(columns="ts")
    return df


def fetch_data():
    """优先从 OKX 永续合约拉取真实 K线，失败/超时则回退到合成数据。"""
    if not HAVE_CCXT:
        print("未安装 ccxt，使用合成数据回测。")
        return generate_synthetic_data()
    box = {}

    def worker():
        try:
            box["df"] = _fetch_okx()
        except Exception as e:
            box["err"] = e

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout=20)
    try:
        if t.is_alive():
            raise TimeoutError("OKX 数据抓取超过 20s 未完成")
        if "err" in box:
            raise box["err"]
        df = box["df"]
        print("已获取 OKX 真实数据：{} 根 {} K线（{} ~ {}）".format(
            len(df), TIMEFRAME, df.index[0].date(), df.index[-1].date()))
        return df
    except Exception as e:
        print("获取真实数据失败（{}），改用合成数据回测。".format(e))
        return generate_synthetic_data()


def generate_synthetic_data():
    """生成几何布朗运动 + 跳跃的合成 BTC 行情，保证框架可独立运行。"""
    np.random.seed(42)
    bars = DAYS * 6  # 每天 6 根 4h K线
    dt = 1.0 / (6 * 365)  # 每根 4h K线占一年的比例
    mu, sigma = 0.25, 0.55  # 年化漂移 / 年化波动率
    start_price = 65000.0
    rets = np.random.normal(mu * dt, sigma * np.sqrt(dt), bars)
    rets += np.random.choice([0, 0, 0, 0, -0.04, 0.04], bars)  # 偶发跳空
    close = start_price * np.exp(np.cumsum(rets))
    open_ = np.concatenate([[close[0]], close[:-1]])
    intrabar = np.abs(np.random.normal(0, 0.008, bars))
    high = np.maximum(open_, close) * (1 + intrabar)
    low = np.minimum(open_, close) * (1 - intrabar)
    volume = np.random.lognormal(10, 1, bars)
    idx = pd.date_range(end=pd.Timestamp.now().floor("h"), periods=bars, freq="4h")
    return pd.DataFrame({"open": open_, "high": high, "low": low,
                         "close": close, "volume": volume}, index=idx)


# ---------------- 技术指标 ----------------
def calc_macd(close, fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL):
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd = ema_fast - ema_slow
    sig = macd.ewm(span=signal, adjust=False).mean()
    hist = macd - sig
    return macd, sig, hist


def generate_signals(df):
    """MACD 水下金叉→死叉→金叉（第二个金叉值高于第一个）→ 做多。"""
    df = df.copy()
    macd, sig, hist = calc_macd(df["close"])
    df["macd"], df["signal_line"], df["hist"] = macd, sig, hist
    df["swing_low"] = df["low"].rolling(SWING_LOOKBACK).min()

    prev_macd = macd.shift()
    prev_sig = sig.shift()
    cross_up = (macd > sig) & (prev_macd <= prev_sig)   # 金叉
    cross_dn = (macd < sig) & (prev_macd >= prev_sig)   # 死叉
    under_water = (macd < 0) & (sig < 0)                # 水下（零轴下方）

    df["signal"] = 0
    # 状态机：0=找金叉1，1=已金叉1等死叉，2=已死叉等金叉2
    state = 0
    first_val = np.nan
    sig_pos = df.columns.get_loc("signal")
    for i in range(len(df)):
        if pd.isna(macd.iloc[i]) or pd.isna(sig.iloc[i]) or pd.isna(prev_macd.iloc[i]):
            continue
        # 买入信号一旦出现，本 bar 不再处理状态转移
        if cross_up.iloc[i] and under_water.iloc[i]:
            if state == 0:
                state = 1
                first_val = macd.iloc[i]
            elif state == 2:
                if macd.iloc[i] > first_val:
                    df.iat[i, sig_pos] = 1   # 第二个金叉高于第一个 → 买入
                # 无论是否买入，重置状态机寻找下一组
                state = 1
                first_val = macd.iloc[i]
        elif cross_dn.iloc[i] and state == 1:
            state = 2
    return df


# ---------------- 回测引擎 ----------------
def run_backtest(df):
    pos = 0                  # 0 空仓 / 1 多（仅做多）
    entry_price = qty = sl = tp = liq_price = 0.0
    cur_trade = None
    equity = INIT_CASH
    bar_count = 0
    trades = []
    equity_curve = np.full(len(df), np.nan)

    def close_position(exit_price, reason, exit_time):
        nonlocal pos, qty, entry_price, sl, tp, liq_price, equity, cur_trade
        pnl = (exit_price - entry_price) * qty          # 仅做多
        exit_fee = qty * exit_price * FEE_RATE
        equity += pnl - exit_fee
        ret = (exit_price - entry_price) / entry_price * LEVERAGE
        cur_trade.update({
            "exit_time": exit_time, "exit_price": round(exit_price, 2),
            "reason": reason, "pnl": round(pnl - exit_fee, 2),
            "return": round(ret, 4),
            "bars_held": bar_count - cur_trade["entry_bar"],
        })
        trades.append(cur_trade)
        cur_trade = None
        pos = 0
        qty = sl = tp = liq_price = entry_price = 0.0

    for i, r in enumerate(df.itertuples()):
        bar_count = i
        price, high, low = r.close, r.high, r.low

        # 1) 持仓中：先查强平(100x≈1%)，再止损，最后止盈
        if pos == 1:
            if low <= liq_price:
                close_position(liq_price, "强平(100x)", r.Index)
            elif low <= sl:
                close_position(sl, "止损", r.Index)
            elif high >= tp:
                close_position(tp, "止盈", r.Index)

        # 2) 资金费率（每 8h = 每 2 根 4h K线收取一次，多头付费）
        if pos == 1 and i % 2 == 0:
            equity -= qty * price * FUNDING_RATE

        # 3) 开仓信号（仅空仓时入场，仅做多）
        if pos == 0 and r.signal == 1 and not np.isnan(r.swing_low):
            sl_price = r.swing_low                    # 止损：阶段性低点
            risk = price - sl_price                    # 止损距离
            if risk <= 0:
                pass  # 低点高于现价，跳过
            else:
                margin = equity * RISK_FRAC            # 1% 本金作保证金
                notional = margin * LEVERAGE           # 名义价值 = 保证金×100
                qty = notional / price
                entry_price = price
                sl = sl_price                          # 止损 = 阶段性低点
                tp = price + RR_RATIO * risk           # 止盈 = 入场 + 3×止损距离
                liq_price = price * (1 - 1.0 / LEVERAGE)  # 强平价 ≈ -1%
                equity -= notional * FEE_RATE          # 开仓手续费
                pos = 1
                cur_trade = {
                    "entry_time": r.Index, "entry_price": round(entry_price, 2),
                    "side": 1, "entry_bar": i,
                }

        equity_curve[i] = equity

    # 收尾：期末按收盘价平掉未平仓位
    if pos == 1:
        close_position(df["close"].iloc[-1], "期末平仓", df.index[-1])

    df = df.copy()
    df["equity"] = equity_curve
    return df, trades


# ---------------- 绩效统计 ----------------
def calc_metrics(df, trades):
    eq = df["equity"].dropna()
    final = eq.iloc[-1]
    total_ret = (final - INIT_CASH) / INIT_CASH
    ann_ret = (1 + total_ret) ** (365.0 / DAYS) - 1

    bars_per_year = 6 * 365  # 4h K线
    rets = eq.pct_change().dropna()
    sharpe = (rets.mean() / rets.std()) * np.sqrt(bars_per_year) if rets.std() > 0 else 0.0

    peak = eq.cummax()
    drawdown = (eq - peak) / peak
    max_dd = drawdown.min()

    n_trades = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    win_rate = len(wins) / n_trades if n_trades else 0.0
    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in trades if t["pnl"] < 0)
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else np.inf
    avg_win = np.mean([t["pnl"] for t in wins]) if wins else 0.0
    avg_loss = np.mean([t["pnl"] for t in trades if t["pnl"] < 0]) or 0.0

    return {
        "初始资金": INIT_CASH, "最终权益": round(final, 2),
        "总收益率": "{:.2%}".format(total_ret), "年化收益率": "{:.2%}".format(ann_ret),
        "最大回撤": "{:.2%}".format(max_dd), "夏普比率": round(sharpe, 3),
        "总交易次数": n_trades, "胜率": "{:.2%}".format(win_rate),
        "利润因子": round(profit_factor, 3), "盈亏比": round(avg_win / abs(avg_loss), 3) if avg_loss else np.inf,
        "平均盈利": round(avg_win, 2), "平均亏损": round(avg_loss, 2),
    }


# ---------------- 可视化 ----------------
def plot_results(df, trades, out_path="backtest_result.png"):
    fig, axes = plt.subplots(3, 1, figsize=(14, 10), sharex=True,
                             gridspec_kw={"height_ratios": [3, 1.2, 1.2]})

    # 1) 价格 + 做多信号点
    ax = axes[0]
    ax.plot(df.index, df["close"], color="#333333", lw=1, label="收盘价")
    longs = [t for t in trades if t["side"] == 1]
    if longs:
        ax.scatter([t["entry_time"] for t in longs], [t["entry_price"] for t in longs],
                   marker="^", color="red", s=40, zorder=5, label="做多")
    ax.set_ylabel("价格 (USDT)")
    ax.legend(loc="upper left", fontsize=8)
    ax.set_title("BTC/USDT 永续合约 MACD策略回测 ({}x杠杆, {}天)".format(LEVERAGE, DAYS))

    # 2) 净值曲线
    ax = axes[1]
    ax.plot(df.index, df["equity"], color="#1f77b4", lw=1.2, label="权益")
    ax.axhline(INIT_CASH, color="gray", ls="--", lw=0.8)
    ax.set_ylabel("权益 (USDT)")
    ax.legend(loc="upper left", fontsize=8)

    # 3) 回撤曲线
    ax = axes[2]
    eq = df["equity"].dropna()
    drawdown = (eq - eq.cummax()) / eq.cummax()
    ax.fill_between(drawdown.index, drawdown * 100, 0, color="#d62728", alpha=0.5)
    ax.set_ylabel("回撤 (%)")
    ax.set_xlabel("时间")

    plt.tight_layout()
    plt.savefig(out_path, dpi=120)
    print("图表已保存：{}".format(out_path))


# ---------------- 主流程 ----------------
def main():
    print("=" * 56)
    print("BTC 永续合约策略回测")
    print("策略：4h MACD 水下金叉→死叉→金叉(第二个更高)做多 | 止损{:.1%} 止盈{:.1%}".format(SL_PCT, TP_PCT))
    print("杠杆：{}x | 保证金比例：{:.0%} | 本金：{}U | 手续费：{:.2%} | 资金费率：{:.3%}/8h".format(
        LEVERAGE, RISK_FRAC, INIT_CASH, FEE_RATE, FUNDING_RATE))
    print("=" * 56)

    df = fetch_data()
    df = generate_signals(df)
    df, trades = run_backtest(df)
    metrics = calc_metrics(df, trades)

    print("-" * 56)
    for k, v in metrics.items():
        print("{:<8}{:>20}".format(k, v))
    print("-" * 56)
    print("交易明细（前 10 笔）：")
    if trades:
        detail = pd.DataFrame(trades)[["entry_time", "entry_price", "exit_price",
                                      "side", "reason", "pnl", "return", "bars_held"]]
        detail["side"] = detail["side"].map({1: "多", -1: "空"})
        print(detail.head(10).to_string(index=False))
    else:
        print("（无交易）")

    plot_results(df, trades)


if __name__ == "__main__":
    main()
