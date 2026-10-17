# -*- coding: utf-8 -*-
from btc_monitor import fetch_klines, check_signal
df = fetch_klines()
print("bars:", len(df))
s, fv, si, macd, sig = check_signal(df)
print("state:", s, "signal_idx:", si, "first_val:", fv)
print("price:", round(df["close"].iloc[-1], 2))
print("macd:", round(macd.iloc[-1], 2), "sig:", round(sig.iloc[-1], 2))
