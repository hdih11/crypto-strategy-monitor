# -*- coding: utf-8 -*-
"""
BTC 永续合约实时监控
======================
策略：4h MACD 水下金叉→死叉→金叉（第二个高于第一个）→ 做多信号
风控：止损 -3%，止盈 +3%（盈亏比 1:1），100x 杠杆，每笔 1% 本金
数据：Binance 现货 API（直接 HTTP，不依赖 ccxt load_markets）
运行：python btc_monitor.py
"""

import sys
import time
import json
import datetime
import urllib.request
import numpy as np
import pandas as pd

# ---------------- 参数 ----------------
SYMBOL = "BTCUSDT"
TIMEFRAME = "4h"
FETCH_BARS = 200
POLL_INTERVAL = 60        # 轮询间隔（秒）
SL_PCT = 0.03             # 止损 -3%
TP_PCT = 0.03             # 止盈 +3%（盈亏比 1:1）
LEVERAGE = 100
INIT_CASH = 1000.0
RISK_FRAC = 0.01
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9

# ---------------- 数据获取 ----------------
# CoinGecko 免费 API：days=30 返回 4h K线（~180 根），适合 MACD 计算
CG_URL = "https://api.coingecko.com/api/v3/coins/bitcoin/ohlc?vs_currency=usd&days=30"


def fetch_klines():
    """从 CoinGecko 获取 4h K线（days=30 → ~180 根）。"""
    req = urllib.request.Request(CG_URL, headers={
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = json.loads(resp.read().decode())
    if not raw:
        raise RuntimeError("CoinGecko 返回空数据")
    # raw = [[ts, open, high, low, close], ...]
    df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close"])
    df["datetime"] = pd.to_datetime(df["ts"], unit="ms")
    df = df[["datetime", "open", "high", "low", "close"]].set_index("datetime")
    df = df.astype(float)
    return df


# ---------------- MACD 计算 ----------------
def calc_macd(close):
    ema_fast = close.ewm(span=MACD_FAST, adjust=False).mean()
    ema_slow = close.ewm(span=MACD_SLOW, adjust=False).mean()
    macd = ema_fast - ema_slow
    sig = macd.ewm(span=MACD_SIGNAL, adjust=False).mean()
    return macd, sig


# ---------------- 信号检测（状态机） ----------------
def check_signal(df):
    """
    MACD 水下金叉→死叉→金叉（第二个高于第一个）→ 做多信号。
    返回 (state, first_val, signal_bar_idx, macd, sig)。
    state: 0=找金叉1, 1=已金叉1等死叉, 2=已死叉等金叉2
    """
    macd, sig = calc_macd(df["close"])
    prev_macd = macd.shift()
    prev_sig = sig.shift()

    cross_up = (macd > sig) & (prev_macd <= prev_sig)
    cross_dn = (macd < sig) & (prev_macd >= prev_sig)
    under_water = (macd < 0) & (sig < 0)

    state = 0
    first_val = np.nan
    signal_idx = -1

    for i in range(len(df)):
        if pd.isna(macd.iloc[i]) or pd.isna(sig.iloc[i]) or pd.isna(prev_macd.iloc[i]):
            continue
        if cross_up.iloc[i] and under_water.iloc[i]:
            if state == 0:
                state = 1
                first_val = macd.iloc[i]
            elif state == 2:
                if not np.isnan(first_val) and macd.iloc[i] > first_val:
                    signal_idx = i
                state = 1
                first_val = macd.iloc[i]
        elif cross_dn.iloc[i] and state == 1:
            state = 2

    return state, first_val, signal_idx, macd, sig


# ---------------- 控制台输出 ----------------
def print_alert(price, macd_val, sig_val):
    """买入信号提示。"""
    sl = price * (1 - SL_PCT)
    tp = price * (1 + TP_PCT)
    liq = price * (1 - 1.0 / LEVERAGE)
    margin = INIT_CASH * RISK_FRAC
    notional = margin * LEVERAGE
    qty = notional / price

    print("\n" + "=" * 56)
    print("            >>> 买入信号触发 <<<")
    print("=" * 56)
    print("  时间      : {}".format(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    print("  当前价格  : {:.2f} USDT".format(price))
    print("  MACD      : {:.4f}".format(macd_val))
    print("  Signal    : {:.4f}".format(sig_val))
    print("-" * 56)
    print("  止损价    : {:.2f}  (-{:.0%})".format(sl, SL_PCT))
    print("  止盈价    : {:.2f}  (+{:.0%})".format(tp, TP_PCT))
    print("  强平价    : {:.2f}  (100x)".format(liq))
    print("  开仓保证金: {:.2f} U (1%)".format(margin))
    print("  名义价值  : {:.2f} U ({}x)".format(notional, LEVERAGE))
    print("  数量      : {:.6f} BTC".format(qty))
    print("  预期亏损  : -{:.2f} U".format(margin))
    print("  预期盈利  : +{:.2f} U".format(margin))
    print("=" * 56 + "\n")
    sys.stdout.write("\a")
    sys.stdout.flush()


def print_status(df, macd, sig, state, first_val):
    """常规状态输出。"""
    price = df["close"].iloc[-1]
    macd_val = macd.iloc[-1]
    sig_val = sig.iloc[-1]
    hist = macd_val - sig_val
    underwater = "是" if (macd_val < 0 and sig_val < 0) else "否"
    state_desc = {0: "等待金叉1", 1: "等死叉(已金叉1)", 2: "等金叉2(已死叉)"}
    now = datetime.datetime.now().strftime("%H:%M:%S")
    fv_str = "首金叉值={:.2f}".format(first_val) if not np.isnan(first_val) else "无"

    print("[{}] 价格={:.2f} | MACD={:.2f} SIG={:.2f} 柱={:.2f} | 水下={} | 状态:{} | {}".format(
        now, price, macd_val, sig_val, hist, underwater,
        state_desc.get(state, "?"), fv_str))


# ---------------- 主循环 ----------------
def run_monitor():
    print("=" * 56)
    print("BTC 永续合约实时监控")
    print("策略: 4h MACD 水下金叉->死叉->金叉(第二个更高) 做多")
    print("风控: 止损-{:.0%} 止盈+{:.0%} (1:1) | {}x杠杆 | 本金{}U".format(
        SL_PCT, TP_PCT, LEVERAGE, INIT_CASH))
    print("数据: Binance {} {} | 轮询 {}s".format(SYMBOL, TIMEFRAME, POLL_INTERVAL))
    print("按 Ctrl+C 退出")
    print("=" * 56 + "\n")

    last_signal_time = None  # 防重复提示

    while True:
        try:
            df = fetch_klines()
            if len(df) < MACD_SLOW + MACD_SIGNAL:
                print("K线不足（{}根），等待...".format(len(df)))
                time.sleep(POLL_INTERVAL)
                continue

            state, fv, sig_idx, macd, sig = check_signal(df)

            # 信号在最新一根 K线出现
            if sig_idx >= 0 and sig_idx == len(df) - 1:
                bar_time = df.index[-1]
                if bar_time != last_signal_time:
                    last_signal_time = bar_time
                    print_alert(df["close"].iloc[-1], macd.iloc[-1], sig.iloc[-1])
                else:
                    print_status(df, macd, sig, state, fv)
            else:
                print_status(df, macd, sig, state, fv)

        except KeyboardInterrupt:
            print("\n监控已停止。")
            break
        except Exception as e:
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            print("[{}] 错误: {}".format(ts, str(e)[:80]))

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run_monitor()
