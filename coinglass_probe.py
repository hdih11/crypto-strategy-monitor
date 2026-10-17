# -*- coding: utf-8 -*-
"""Coinglass V4 端点探测（key 从 coinglass_config.json 读取，不打印密钥）。"""
import json
import requests

cfg = json.load(open("coinglass_config.json", encoding="utf-8"))
BASE = "https://open-api-v4.coinglass.com"
H = {"CG-API-KEY": cfg["api_key"], "User-Agent": "Mozilla/5.0"}

# (名称, 路径, 参数候选) — V4 文档使用连字符路径，参数名按文档常见 coin/interval/limit/range 试探
PROBES = [
    ("支持合约币种", "/api/futures/supported-coins", {}),
    ("恐惧贪婪指数", "/api/index/fear-greed-history", {"limit": 3}),
    ("资金费率OHLC", "/api/futures/funding-rate/history",
     {"coin": "BTC", "interval": "1h", "limit": 3}),
    ("聚合OI", "/api/futures/open-interest/aggregated-history",
     {"coin": "BTC", "interval": "1h", "limit": 3}),
    ("全局多空账户比", "/api/futures/global-long-short-account-ratio/history",
     {"coin": "BTC", "interval": "1h", "limit": 3}),
    ("大户多空比", "/api/futures/top-long-short-account-ratio/history",
     {"coin": "BTC", "interval": "1h", "limit": 3}),
    ("币种爆仓历史", "/api/futures/liquidation/aggregated-history",
     {"coin": "BTC", "interval": "1h", "limit": 3}),
    ("MACD指标", "/api/futures/indicators/macd",
     {"coin": "BTC", "interval": "1h", "limit": 3}),
    ("RSI列表", "/api/futures/rsi/list", {"interval": "1h"}),
    ("BTC dominance", "/api/index/bitcoin-dominance", {}),
]

for name, path, params in PROBES:
    try:
        r = requests.get(BASE + path, headers=H, params=params, timeout=12)
        txt = r.text[:260].replace("\n", " ")
        tag = ""
        try:
            j = r.json()
            tag = " success={}".format(j.get("success"))
            d = j.get("data")
            tag += " data={}".format(len(d) if isinstance(d, list)
                                     else list(d.keys())[:6] if isinstance(d, dict) else d)
        except Exception:
            pass
        print("[{}] HTTP {}{}\n   {}".format(name, r.status_code, tag, txt))
    except Exception as e:
        print("[{}] FAIL: {}".format(name, e))
