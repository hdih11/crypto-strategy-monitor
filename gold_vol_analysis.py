# -*- coding: utf-8 -*-
"""黄金 1h/4h 方差波动率分析（一次性）：定标止损宽度用"""
import sys
import numpy as np

sys.path.insert(0, r"c:\Users\dark\Documents\trae_projects\.trae\skills\btc-eth-macd-backtest\scripts")
import macd_backtest as bt

BARS_PER_YEAR = {"1h": 24 * 365, "4h": 6 * 365}
N = 20  # 纽约开盘起N根


def ny_window(df, tf):
    """COMEX电子盘交易日开盘=北京06:00(夏令时)=UTC前日22:00(周四22:00起)。
    1h从开盘整点(UTC 22:00)起取N根；4h从覆盖开盘的4h根(UTC周四20:00)起取N根。"""
    idx = df.index
    if tf == "1h":
        t0 = idx[(idx.dayofweek == 3) & (idx.hour == 22)].max()
    else:
        t0 = idx[(idx.dayofweek == 3) & (idx.hour == 20)].max()
    sub = df[idx >= t0].iloc[:N]
    return sub, sub.index[0], sub.index[-1]


for tf in ("1h", "4h"):
    df, s, e = ny_window(bt.fetch_klines("PAXGUSDT", tf, 90), tf)
    ret = np.log(df["close"]).diff().dropna()
    sigma = ret.std()                      # 单根对数收益标准差
    var = ret.var()                        # 方差
    ann_vol = sigma * np.sqrt(BARS_PER_YEAR[tf])
    range_pct = ((df["high"] - df["low"]) / df["close"]).mean()
    print("[{}] 纽约开盘起{}根(UTC {} ~ {}) 单根sigma={:.4%} 方差={:.2e} 年化波动={:.1%} 平均振幅={:.3%}".format(
        tf, N, s.strftime("%m-%d %H:%M"), e.strftime("%m-%d %H:%M"),
        sigma, var, ann_vol, range_pct))
    for k, label in ((1.0, "1σ"), (1.5, "1.5σ"), (2.0, "2σ")):
        print("    止损={}={:.3%} -> 1:1止盈={:.3%}".format(label, k * sigma, k * sigma))
