# -*- coding: utf-8 -*-
"""
黄金 关键位反转+MACD序列确认 实时监控 + 模拟盘
================================================
策略与 gold_swing_backtest.py 定稿回测(1h信号周期)完全一致:
  做多: 价格触及下方摆动低点±0.15% 且 1h MACD 最近三次交叉=金叉→死叉→金叉
  做空: 价格触及上方摆动高点±0.15% 且 1h MACD 最近三次交叉=死叉→金叉→死叉
  止损 = max(近10根1h 2sigma, 0.15%)  止盈 = 3倍(1:3)
  关键位: 日线摆动高低点(左右各2根), 有效期15交易日, 收盘穿越±0.15%失效
资金口径: 1000U 本金, 100U x 100x 全仓, 手续费0.04%/边, 资金费0.125U/根(1h), 强平约-9.5%
状态: paper_state_gold_swing.json / 成交: paper_trades_gold_swing.log
每根1h收盘后8秒检查; 信号+巡检推送微信群; Ctrl+C 停止。
"""
import datetime as dt
import importlib.util
import json
import os
import time

import numpy as np
import pandas as pd

_SKILL = ".trae/skills/btc-eth-macd-backtest/scripts/macd_backtest.py"
_spec = importlib.util.spec_from_file_location("bt_skill", _SKILL)
bt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bt)

SYMBOL = "PAXGUSDT"
TF_SEC = 3600
LOOKBACK_DAYS = 90
STATE_FILE = "paper_state_gold_swing.json"
TRADES_LOG = "paper_trades_gold_swing.log"

MARGIN, LEV = 100.0, 100.0
NOTIONAL = MARGIN * LEV
FEE = 0.0004
FUND_BAR = 0.0001 / 8         # 资金费0.01%/8h -> 0.125U/根(1h)
LIQ_PCT = 0.095
SIGMA_BARS, SIGMA_K, SL_FLOOR = 10, 2.0, 0.0015
RR = 3.0
SWING_N, TOL, MAX_AGE = 2, 0.0015, 15


def log_alert(msg):
    line = "[{}] {}".format(dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print("\n" + "!" * 60)
    print(line)
    print("!" * 60 + "\n")
    with open("signal_alerts.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")
    try:
        import winsound
        for _ in range(3):
            winsound.Beep(1200, 250)
            time.sleep(0.1)
    except Exception:
        pass
    try:
        import threading
        import wechat_notify
        def _push():
            try:
                _, errs = wechat_notify.send("黄金关键位反转监控提醒", line)
                if errs:
                    print("微信推送失败: {}".format("; ".join(errs)))
                else:
                    print("[微信群推送] 已发送")
            except Exception as e:
                print("微信推送异常: {}".format(e))
        threading.Thread(target=_push, daemon=True).start()
    except Exception:
        pass


def status_notify(msg):
    """巡检状态推送(蜂鸣不响, 只推微信+控制台)"""
    line = "[{}] {}".format(dt.datetime.now().strftime("%m-%d %H:%M"), msg)
    print(line)
    try:
        import threading
        import wechat_notify
        def _push():
            try:
                _, errs = wechat_notify.send("黄金策略巡检", msg)
                if errs:
                    print("微信推送失败: {}".format("; ".join(errs)))
                else:
                    print("[微信群推送] 巡检已发送")
            except Exception:
                pass
        threading.Thread(target=_push, daemon=True).start()
    except Exception:
        pass


def closed_bars(df):
    now_ms = int(time.time() * 1000)
    last_open_ms = int(df.index[-1].timestamp() * 1000)
    if now_ms < last_open_ms + TF_SEC * 1000:
        return df.iloc[:-1]
    return df


def build_levels(daily):
    """日线摆动高低点 -> 关键位列表"""
    levels = []
    for i in range(SWING_N, len(daily) - SWING_N):
        seg_h = daily["h"].iloc[i - SWING_N:i + SWING_N + 1]
        seg_l = daily["l"].iloc[i - SWING_N:i + SWING_N + 1]
        d0 = daily.index[i]
        if daily["h"].iloc[i] == seg_h.max():
            levels.append({"price": float(daily["h"].iloc[i]), "kind": "res", "born": d0.date().isoformat()})
        if daily["l"].iloc[i] == seg_l.min():
            levels.append({"price": float(daily["l"].iloc[i]), "kind": "sup", "born": d0.date().isoformat()})
    return levels


def macd_last_seq(close):
    """返回截至最后一根收盘K线的最近3次MACD交叉符号序列, 如 [1,-1,1]; 不足3次为 []"""
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif, dea = ema12 - ema26, (ema12 - ema26).ewm(span=9, adjust=False).mean()
    above = (dif > dea).values
    evs = []
    for i in range(1, len(above)):
        if above[i] and not above[i - 1]:
            evs.append(1)
        elif not above[i] and above[i - 1]:
            evs.append(-1)
    return evs[-3:]


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"init": 1000.0, "balance": 1000.0, "position": None, "levels": {},
            "processed": []}


def save_state(st):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)


def log_trade(msg):
    line = "[{}] {}".format(dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    with open(TRADES_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print("    " + line)


def check():
    st = load_state()
    df = closed_bars(bt.fetch_klines(SYMBOL, "1h", LOOKBACK_DAYS))
    close = df["close"]
    last, last_ts = float(close.iloc[-1]), df.index[-1]
    cur_tday = (last_ts + pd.Timedelta(hours=2)).date()

    # 关键位: 重算90天摆动位并与持久化状态合并(保留used/dead)
    daily = df.groupby((df.index + pd.Timedelta(hours=2)).normalize()).agg(
        o=("open", "first"), h=("high", "max"), l=("low", "min"), c=("close", "last"))
    fresh = build_levels(daily)
    merged = {}
    for lv in fresh:
        key = "{:.2f}|{}".format(lv["price"], lv["born"])
        old = st["levels"].get(key, {})
        merged[key] = {"price": lv["price"], "kind": lv["kind"], "born": lv["born"],
                       "used": old.get("used", False), "dead": old.get("dead", False)}
    st["levels"] = merged

    # 失效/过期标记
    for lv in merged.values():
        if lv["used"] or lv["dead"]:
            continue
        age = (cur_tday - dt.date.fromisoformat(lv["born"])).days
        if age > MAX_AGE:
            lv["dead"] = True
        elif lv["kind"] == "res" and last > lv["price"] * (1 + TOL):
            lv["dead"] = True
        elif lv["kind"] == "sup" and last < lv["price"] * (1 - TOL):
            lv["dead"] = True

    pos = st["position"]

    # 1) 持仓: 对入场后新收盘K线逐根判定出场(口径=回测引擎)
    if pos:
        entry_ts = pd.Timestamp(pos["entry_time"])
        future = df[df.index > entry_ts]
        exit_info = None
        # 只对上次检查后新收盘的K线累计资金费，避免同一根K线每轮重复计入
        for ts2, row in future.iloc[int(pos.get("bars", 0)):].iterrows():
            pos["fund_acc"] += NOTIONAL * FUND_BAR
            o, h, l = float(row["open"]), float(row["high"]), float(row["low"])
            side = pos["side"]
            exit_price, reason = None, None
            if side == 1:
                if o <= pos["liq"]:
                    exit_price, reason = pos["liq"], "强平"
                elif o < pos["sl"]:
                    exit_price, reason = o, "止损"
                elif pos["liq"] > pos["sl"] and l <= pos["liq"]:
                    exit_price, reason = pos["liq"], "强平"
                elif l <= pos["sl"]:
                    exit_price, reason = pos["sl"], "止损"
                elif h >= pos["tp"]:
                    exit_price, reason = pos["tp"], "止盈"
            else:
                if o >= pos["liq"]:
                    exit_price, reason = pos["liq"], "强平"
                elif o > pos["sl"]:
                    exit_price, reason = o, "止损"
                elif pos["liq"] < pos["sl"] and h >= pos["liq"]:
                    exit_price, reason = pos["liq"], "强平"
                elif h >= pos["sl"]:
                    exit_price, reason = pos["sl"], "止损"
                elif l <= pos["tp"]:
                    exit_price, reason = pos["tp"], "止盈"
            if exit_price is not None:
                exit_info = (ts2, exit_price, reason)
                break
        if exit_info:
            ts2, exit_price, reason = exit_info
            chg = (exit_price - pos["entry"]) / pos["entry"] if pos["side"] == 1 \
                else (pos["entry"] - exit_price) / pos["entry"]
            pnl = NOTIONAL * chg - pos["entry_fee"] - NOTIONAL * FEE - pos["fund_acc"]
            st["balance"] += pnl
            log_trade("[模拟平仓] {} {} @{:.2f}->{:.2f} 盈亏{:+.2f}U 余额{:.2f}U".format(
                reason, "多" if pos["side"] == 1 else "空", pos["entry"], exit_price,
                pnl, st["balance"]))
            log_alert("[模拟盘平仓] {} {} 出场价 {:.2f} 盈亏 {:+.2f}U | 余额 {:.2f}U".format(
                "多" if pos["side"] == 1 else "空", reason, exit_price, pnl, st["balance"]))
            st["position"] = None
            pos = None
        else:
            pos["bars"] = int(len(future))

    # 2) 无持仓: 触位+序列确认 -> 开仓
    sig_txt = ""
    if pos is None:
        seq = macd_last_seq(close)
        seq_txt = "→".join("金叉" if s == 1 else "死叉" for s in seq) if seq else "不足3次"
        touched = None
        for lv in merged.values():
            if lv["used"] or lv["dead"]:
                continue
            top, bot = lv["price"] * (1 + TOL), lv["price"] * (1 - TOL)
            if float(df["low"].iloc[-1]) <= top and float(df["high"].iloc[-1]) >= bot:
                touched = lv
                break
        if touched is not None:
            side = "short" if touched["kind"] == "res" else "long"
            need = [-1, 1, -1] if side == "short" else [1, -1, 1]
            if seq == need:
                proc_key = "{}|{:.2f}|{}".format(last_ts.isoformat(), touched["price"], side)
                if proc_key not in st["processed"]:
                    st["processed"].append(proc_key)
                    st["processed"] = st["processed"][-300:]
                    sig_hist = np.log(close).diff()
                    sig = float(sig_hist.iloc[-SIGMA_BARS:].std())
                    sl_d = max(SIGMA_K * sig, SL_FLOOR)
                    price = last
                    sign = 1 if side == "long" else -1
                    sl = price * (1 - sign * sl_d)
                    tp = price * (1 + sign * sl_d * RR)
                    liq = price * (1 - sign * LIQ_PCT)
                    st["position"] = {"side": 1 if side == "long" else -1,
                                      "entry": price, "entry_time": last_ts.isoformat(),
                                      "sl": sl, "tp": tp, "liq": liq,
                                      "entry_fee": NOTIONAL * FEE, "fund_acc": 0.0,
                                      "bars": 0}
                    touched["used"] = True
                    tag = "支撑位做多" if side == "long" else "阻力位做空"
                    log_trade("[模拟开仓] {} @{:.2f} 关键位{:.2f} 止损{:.2f}({:.2f}%) 止盈{:.2f}".format(
                        tag, price, touched["price"], sl, sl_d * 100, tp))
                    log_alert("[买卖点信号] PAXG {} @ {:.2f} | 关键位 {:.2f} | 止损 {:.2f} 止盈 {:.2f} | 1:3".format(
                        tag, price, touched["price"], sl, tp))
            else:
                sig_txt = "触{}{:.0f} 但序列[{}]不匹配".format(
                    "阻" if touched["kind"] == "res" else "支", touched["price"], seq_txt)

    # 3) 巡检推送
    pos = st["position"]
    unreal = 0.0
    if pos:
        chg = (last - pos["entry"]) / pos["entry"] * pos["side"]
        unreal = NOTIONAL * chg
    act_res = ["{:.0f}".format(lv["price"]) for lv in merged.values()
               if lv["kind"] == "res" and not lv["used"] and not lv["dead"]]
    act_sup = ["{:.0f}".format(lv["price"]) for lv in merged.values()
               if lv["kind"] == "sup" and not lv["used"] and not lv["dead"]]
    seq = macd_last_seq(close)
    seq_txt = "→".join("金叉" if s == 1 else "死叉" for s in seq) if seq else "不足3次"
    pos_txt = "持仓{}@{:.2f} 浮盈{:+.1f}U".format(
        "多" if pos["side"] == 1 else "空", pos["entry"], unreal) if pos else "空仓"
    eq = st["balance"] + unreal
    status_notify(
        "PAXG {:.2f} | MACD最近3叉: {} | 活跃阻力[{}] 支撑[{}] | {} | 权益 {:.1f}U ({:+.1f}%)".format(
            last, seq_txt, ",".join(act_res[-3:]) or "无", ",".join(act_sup[-3:]) or "无",
            pos_txt, eq, (eq / st["init"] - 1) * 100))

    save_state(st)


def main():
    print("=" * 64)
    print("黄金 关键位反转+MACD序列确认 监控 (模拟盘) 启动")
    print("标的: {} 1h | 止损=max(10根2σ,0.15%) 止盈1:3 | 100U x 100x 全仓".format(SYMBOL))
    print("=" * 64)
    while True:
        try:
            check()
        except Exception as e:
            print("[检查异常] {}".format(e))
        now = time.time()
        nxt = (int(now // TF_SEC) + 1) * TF_SEC + 8     # 下个整点过8秒
        wait = max(nxt - now, 1)
        print("[等待 {:.0f}s 后下一次检查]".format(wait))
        time.sleep(wait)


if __name__ == "__main__":
    main()
