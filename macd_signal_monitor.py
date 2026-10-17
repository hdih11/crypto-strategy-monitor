# -*- coding: utf-8 -*-
"""
BTC/ETH MACD 二次交叉形态 实时监控 + 模拟盘 / Gate实盘
======================================================
策略与回测技能 btc-eth-macd-backtest 完全一致（直接复用其函数）:
  买点: DIF 零轴水下 金叉1 -> 死叉 -> 金叉2，且金叉2的DIF值高于金叉1
  卖点(做空): DIF 零轴水上 死叉1 -> 金叉 -> 死叉2，且死叉2的DIF值低于死叉1

三种模式（默认模拟盘）:
  python macd_signal_monitor.py                 # paper 模拟盘：模拟成交/止损止盈/资金费/权益
  python macd_signal_monitor.py --signal-only   # 仅信号提示，不做模拟成交
  python macd_signal_monitor.py --live          # Gate.io 实盘（需 gate_config.json）

模拟盘资金口径与回测严格一致：每币独立1000U，100U保证金x100=10000U名义，全仓，
手续费0.04%/边，资金费0.01%/8h按周期折算；出场按"止损/强平谁近先触"，开盘跳空滑点。
状态持久化 paper_state.json，重启不丢持仓；成交记录 paper_trades.log。

每根K线收盘后检查一次；Ctrl+C 停止。
"""
import argparse
import datetime as dt
import importlib.util
import json
import os
import sys
import time

import numpy as np
import pandas as pd

# 复用技能脚本中的取数/MACD/信号函数与资金常量，保证与回测口径一致
_SKILL = ".trae/skills/btc-eth-macd-backtest/scripts/macd_backtest.py"
_spec = importlib.util.spec_from_file_location("bt_skill", _SKILL)
bt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bt)

TF_SECONDS = {"15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
# 状态机需要的回溯天数（覆盖足够形态历史）
LOOKBACK_DAYS = {"15m": 20, "1h": 40, "4h": 90, "1d": 365}
STATE_FILE = "paper_state.json"
TRADES_LOG = "paper_trades.log"


def _file_paths(tag):
    """同一脚本可并行运行多个周期/参数实例，状态文件按 tag 隔离。"""
    if not tag:
        return STATE_FILE, TRADES_LOG
    return "paper_state_{}.json".format(tag), "paper_trades_{}.log".format(tag)


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
    # 微信推送（异步，不阻塞监控主循环；未配置 wechat_config.json 时自动跳过）
    try:
        import threading
        import wechat_notify
        def _push():
            try:
                _, errs = wechat_notify.send("BTC/ETH MACD 监控提醒", line)
                if errs:
                    print("微信推送失败: {}".format("; ".join(errs)))
                else:
                    print("[微信群推送] 已发送")
            except Exception as e:
                print("微信推送异常: {}".format(e))
        threading.Thread(target=_push, daemon=True).start()
    except Exception:
        pass


def pattern_status(df):
    """走完一遍状态机，返回当前各方向形态进度（复刻技能中的状态机）。"""
    dif, dea = df["dif"].values, df["dea"].values
    l_state, l_first, s_state, s_first = 0, np.nan, 0, np.nan
    for i in range(1, len(df)):
        if np.isnan(dif[i]) or np.isnan(dea[i]):
            continue
        golden = dif[i - 1] <= dea[i - 1] and dif[i] > dea[i]
        death = dif[i - 1] >= dea[i - 1] and dif[i] < dea[i]
        if dif[i] >= 0:
            l_state, l_first = 0, np.nan
        elif golden:
            if l_state == 0:
                l_state, l_first = 1, dif[i]
            elif l_state == 2:
                l_state, l_first = 1, dif[i]
        elif death and l_state == 1:
            l_state = 2
        if dif[i] <= 0:
            s_state, s_first = 0, np.nan
        elif death:
            if s_state == 0:
                s_state, s_first = 1, dif[i]
            elif s_state == 2:
                s_state, s_first = 1, dif[i]
        elif golden and s_state == 1:
            s_state = 2
    l_txt = {0: "无形态", 1: "金叉1已现(DIF1={:.4g})，等死叉".format(l_first),
             2: "死叉已现，等金叉2(需DIF>{:.4g})".format(l_first)}[l_state]
    s_txt = {0: "无形态", 1: "死叉1已现(DIF1={:.4g})，等金叉".format(s_first),
             2: "金叉已现，等死叉2(需DIF<{:.4g})".format(s_first)}[s_state]
    return l_txt, s_txt


def closed_bars(df, tf_sec):
    """剔除尚未收盘的最后一根K线（Binance返回含进行中K线）。"""
    now_ms = int(time.time() * 1000)
    last_open_ms = int(df.index[-1].timestamp() * 1000)
    if now_ms < last_open_ms + tf_sec * 1000:
        return df.iloc[:-1]
    return df


class PaperBroker:
    """多信号流共享一个资金池的纸面账户。

    持仓按"币种|周期"为 key（如 ETHUSDT|1h 与 ETHUSDT|4h 可同时各持一仓），
    所有仓位共用同一笔资金；每开一仓冻结 100U 保证金，可用保证金不足则跳过。
    每仓自带各自周期的止损/止盈/资金费率；出场口径与回测引擎一致。
    注：多仓并存时强平按入场时资金池权益逐仓近似（与交易所组合强平略有差异）。
    """

    def __init__(self, pool, state_file=STATE_FILE, trades_log=TRADES_LOG):
        self.state_file, self.trades_log = state_file, trades_log
        if os.path.exists(state_file):
            with open(state_file, "r", encoding="utf-8") as f:
                self.state = json.load(f)
        else:
            self.state = {"init": pool, "balance": pool, "positions": {}, "processed": []}

    def save(self):
        tmp = self.state_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.state_file)

    def _log(self, msg):
        line = "[{}] {}".format(dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
        with open(self.trades_log, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        print("    " + line)

    @staticmethod
    def _exit_hit(pos, o, h, l):
        """复刻回测引擎：止损/强平谁离入场更近先触；开盘跳空滑点；同根双触按止损。"""
        sl, tp, liq, side = pos["sl"], pos["tp"], pos["liq"], pos["side"]
        if side == 1:
            if o <= liq:
                return liq, "强平"
            if o < sl:
                return o, "止损"
            if liq > sl and l <= liq:
                return liq, "强平"
            if l <= sl:
                return sl, "止损"
            if h >= tp:
                return tp, "止盈"
        else:
            if o >= liq:
                return liq, "强平"
            if o > sl:
                return o, "止损"
            if liq < sl and h >= liq:
                return liq, "强平"
            if h >= sl:
                return sl, "止损"
            if l <= tp:
                return tp, "止盈"
        return None, None

    def _close(self, key, label, pos, exit_price, reason):
        side = pos["side"]
        if reason == "强平":
            pnl = -(pos["entry_balance"] - bt.NOTIONAL * bt.MAINT_RATE)
        else:
            chg = (exit_price - pos["entry"]) / pos["entry"] if side == 1 \
                else (pos["entry"] - exit_price) / pos["entry"]
            pnl = bt.NOTIONAL * chg
        pnl -= pos["fund_acc"]
        pnl -= pos["entry_fee"] + bt.NOTIONAL * bt.FEE_RATE
        self.state["balance"] += pnl
        del self.state["positions"][key]
        self._log("[模拟平仓] {} {} {} @{:.2f}->{:.2f} 盈亏{:+.2f}U 资金池{:.2f}U".format(
            label, "多" if side == 1 else "空", reason, pos["entry"], exit_price,
            pnl, self.state["balance"]))

    def update_stream(self, key, label, df, sl_pct, tp_pct, fund_per_bar):
        pos = self.state["positions"].get(key)

        # 1) 持仓：对入场后新收盘的K线逐根判定
        if pos is not None:
            entry_ts = pd.Timestamp(pos["entry_time"])
            future = df[df.index > entry_ts]
            # 只对上次检查后新收盘的K线累计资金费，避免同一根K线每轮重复计入
            for ts2, row in future.iloc[int(pos.get("bars", 0)):].iterrows():
                pos["fund_acc"] += bt.NOTIONAL * fund_per_bar * pos["side"]
                exit_price, reason = self._exit_hit(pos, row["open"], row["high"], row["low"])
                if exit_price is not None:
                    self._close(key, label, pos, exit_price, reason)
                    pos = None
                    break
            if pos is not None:
                pos["bars"] = int(len(future))
                self.state["positions"][key] = pos

        # 2) 无持仓：最新已收盘K线出现未处理信号则开仓（资金池共享，需有100U空闲保证金）
        last_ts = df.index[-1]
        proc_key = "{}|{}".format(key, last_ts.isoformat())
        if key not in self.state["positions"] and int(df["signal"].iloc[-1]) != 0 \
                and proc_key not in self.state["processed"]:
            self.state["processed"].append(proc_key)
            self.state["processed"] = self.state["processed"][-300:]
            side = int(df["signal"].iloc[-1])
            price = float(df["close"].iloc[-1])
            bal = self.state["balance"]
            used = bt.MARGIN * len(self.state["positions"])  # 每仓冻结100U
            free = bal - used
            if free < bt.MARGIN + bt.NOTIONAL * bt.FEE_RATE:
                self._log("[跳过开仓] {} 资金池{:.2f}U 已用保证金{:.0f}U 空闲{:.2f}U不足".format(
                    label, bal, used, free))
            else:
                liq_dist = max((bal - bt.NOTIONAL * bt.MAINT_RATE) / bt.NOTIONAL, 0)
                if side == 1:
                    sl, tp, liq = price * (1 - sl_pct), price * (1 + tp_pct), price * (1 - liq_dist)
                else:
                    sl, tp, liq = price * (1 + sl_pct), price * (1 - tp_pct), price * (1 + liq_dist)
                self.state["positions"][key] = {
                    "side": side, "entry": price, "entry_time": last_ts.isoformat(),
                    "sl": sl, "tp": tp, "liq": liq, "entry_balance": bal,
                    "entry_fee": bt.NOTIONAL * bt.FEE_RATE, "fund_acc": 0.0, "bars": 0}
                self._log("[模拟开仓] {} {} @{:.2f} 止损{:.2f} 止盈{:.2f} 资金池{:.2f}U 持仓{}个".format(
                    label, "多" if side == 1 else "空", price, sl, tp, bal,
                    len(self.state["positions"])))
                log_alert("[模拟盘成交] {} {} {} @ {:.2f} | 止损 {:.2f} 止盈 {:.2f}".format(
                    label.split()[0], label.split()[1] if " " in label else "",
                    "做多" if side == 1 else "做空", price, sl, tp))
        self.save()

    def equity(self, prices):
        """prices: {stream_key: 最新价} -> (总权益, 浮盈合计, 持仓明细列表)"""
        total_unreal, items = 0.0, []
        for key, pos in self.state["positions"].items():
            price = prices.get(key)
            if price is None:
                continue
            chg = (price - pos["entry"]) / pos["entry"] * pos["side"]
            unreal = bt.NOTIONAL * chg - pos["entry_fee"] - bt.NOTIONAL * bt.FEE_RATE
            total_unreal += unreal
            items.append((key, pos, unreal))
        return self.state["balance"] + total_unreal, total_unreal, items


def parse_watch(spec):
    """解析 'ETHUSDT:1h:0.01:0.02,ETHUSDT:4h:0.03:0.03' 形式的信号流列表。"""
    out = []
    for part in spec.split(","):
        p = part.strip().split(":")
        if len(p) != 4:
            raise ValueError("--watch 格式应为 币种:周期:止损:止盈，多项逗号分隔")
        sym, tf, sl, tp = p[0].strip().upper(), p[1].strip(), float(p[2]), float(p[3])
        if tf not in TF_SECONDS:
            raise ValueError("不支持的周期: {}".format(tf))
        out.append({"sym": sym, "tf": tf, "sl": sl, "tp": tp,
                    "key": "{}|{}".format(sym, tf)})
    return out


def pattern_detail(df):
    """与 pattern_status 同源，返回含完整形态链与触发阈值的详细描述。"""
    dif, dea = df["dif"].values, df["dea"].values
    l_state, l_first, s_state, s_first = 0, np.nan, 0, np.nan
    for i in range(1, len(df)):
        if np.isnan(dif[i]) or np.isnan(dea[i]):
            continue
        golden = dif[i - 1] <= dea[i - 1] and dif[i] > dea[i]
        death = dif[i - 1] >= dea[i - 1] and dif[i] < dea[i]
        if dif[i] >= 0:
            l_state, l_first = 0, np.nan
        elif golden:
            if l_state == 0:
                l_state, l_first = 1, dif[i]
            elif l_state == 2:
                l_state, l_first = 1, dif[i]
        elif death and l_state == 1:
            l_state = 2
        if dif[i] <= 0:
            s_state, s_first = 0, np.nan
        elif death:
            if s_state == 0:
                s_state, s_first = 1, dif[i]
            elif s_state == 2:
                s_state, s_first = 1, dif[i]
        elif golden and s_state == 1:
            s_state = 2
    long_chain = {
        0: "未启动",
        1: "金叉1(DIF1={:.4g}) → [等死叉] → 金叉2需DIF>{:.4g}".format(l_first, l_first),
        2: "金叉1(DIF1={:.4g}) → 死叉已现 → [等金叉2，需DIF>{:.4g}]".format(l_first, l_first),
    }[l_state]
    short_chain = {
        0: "未启动",
        1: "死叉1(DIF1={:.4g}) → [等金叉] → 死叉2需DIF<{:.4g}".format(s_first, s_first),
        2: "死叉1(DIF1={:.4g}) → 金叉已现 → [等死叉2，需DIF<{:.4g}]".format(s_first, s_first),
    }[s_state]
    return long_chain, short_chain


def check_streams(streams, args, seen, broker):
    """执行一轮检测。返回微信群状态快照HTML（每轮检测后推送一次）。"""
    prices = {}
    snap = []  # 微信群状态快照行（HTML）
    # 按周期分组输出：同周期的信号流放在一块，周期从短到长
    order = sorted(set(c["tf"] for c in streams), key=lambda t: TF_SECONDS[t])
    for tf in order:
        group = [c for c in streams if c["tf"] == tf]
        print("=" * 70)
        print("【{} 周期】{} 个监控标的".format(tf, len(group)))
        snap.append("<b>【{} 周期】</b>".format(tf))
        for cfg in group:
            sym = cfg["sym"]
            tf_sec = TF_SECONDS[tf]
            df = bt.fetch_klines(sym, tf, LOOKBACK_DAYS[tf])
            df = closed_bars(df, tf_sec)
            df = bt.add_macd(df)
            df = bt.generate_signals(df)
            if getattr(args, "trend_filter", 0):
                df = bt.apply_trend_filter(df, args.trend_filter)
            last, ts = df.iloc[-1], df.index[-1]
            prices[cfg["key"]] = float(last["close"])
            l_txt, s_txt = pattern_status(df)
            pos_txt = "水上" if last["dif"] > 0 else "水下"
            label = "{} {}".format(sym, tf)
            if tf in ("4h", "1d"):
                # 大周期详细两行：DIF/DEA/MACD柱 + 完整形态链与触发阈值
                l_chain, s_chain = pattern_detail(df)
                print("[{}] {} 收={:<10.2f} DIF={:+.4g} DEA={:+.4g} MACD柱={:+.4g}({}) 止损{:.1%}/止盈{:.1%}".format(
                    ts.strftime("%m-%d %H:%M"), sym, last["close"],
                    last["dif"], last["dea"], last["hist"], pos_txt, cfg["sl"], cfg["tp"]))
                print("         形态: 多头 {} | 空头 {}".format(l_chain, s_chain))
                snap.append("{} {} 收{:.2f} DIF{:+.4g} DEA{:+.4g} 柱{:+.4g}({})".format(
                    sym, tf, last["close"], last["dif"], last["dea"], last["hist"], pos_txt))
                snap.append("　多头：{}<br>　空头：{}".format(l_chain, s_chain))
            else:
                print("[{}] {} 收={:<10.2f} DIF={:+.4g}({}) 止损{:.1%}/止盈{:.1%} | 多: {} | 空: {}".format(
                    ts.strftime("%m-%d %H:%M"), sym, last["close"], last["dif"], pos_txt,
                    cfg["sl"], cfg["tp"], l_txt, s_txt))
                snap.append("{} {} 收{:.2f} DIF{:+.4g}({}) 止损{:.1%}/止盈{:.1%}<br>　多：{} ｜ 空：{}".format(
                    sym, tf, last["close"], last["dif"], pos_txt, cfg["sl"], cfg["tp"], l_txt, s_txt))

            if args.paper:
                fund_per_bar = bt.FUND_8H * {"15m": 1 / 32, "1h": 1 / 8, "4h": 1 / 2, "1d": 3.0}[tf]
                broker.update_stream(cfg["key"], label, df, cfg["sl"], cfg["tp"], fund_per_bar)

            sig = int(last["signal"])
            skey = (cfg["key"], ts)
            if sig != 0 and skey not in seen:
                seen.add(skey)
                side = "买点-做多" if sig == 1 else "卖点-做空"
                price = float(last["close"])
                sl = price * (1 - cfg["sl"]) if sig == 1 else price * (1 + cfg["sl"])
                tp = price * (1 + cfg["tp"]) if sig == 1 else price * (1 - cfg["tp"])
                log_alert("{} {} {} @ {:.2f} | 建议止损 {:.2f} 止盈 {:.2f} | 100U保证金x100 全仓".format(
                    label, tf, side, price, sl, tp))
                if args.live:
                    execute_live(sym, sig, price, sl, tp)

    if args.paper:
        eq, unreal, items = broker.equity(prices)
        init = broker.state["init"]
        print("=" * 70)
        print("【共享资金池】（1h/4h 所有仓位合并）")
        snap.append("<b>【共享资金池】</b>")
        if items:
            for tf in order:
                tf_items = [(k, p, u) for k, p, u in items if k.endswith("|" + tf)]
                if not tf_items:
                    continue
                print("  {} 持仓:".format(tf))
                for key, pos, u in tf_items:
                    print("    {} {} @{:.2f} 浮盈{:+.2f}U 止损{:.2f} 止盈{:.2f}".format(
                        key.split("|")[0], "多" if pos["side"] == 1 else "空",
                        pos["entry"], u, pos["sl"], pos["tp"]))
                    snap.append("　[{}] {} {} @{:.2f} 浮盈{:+.2f}U".format(
                        tf, key.split("|")[0], "多" if pos["side"] == 1 else "空",
                        pos["entry"], u))
        else:
            print("  当前无持仓")
            snap.append("　当前无持仓")
        print("  已实现余额{:.2f}U + 浮盈{:+.2f}U = 权益{:.2f}U (本金{:.0f} {:+.1%})".format(
            broker.state["balance"], unreal, eq, init, eq / init - 1))
        snap.append("💰 余额{:.2f}U + 浮盈{:+.2f} = <b>权益{:.2f}U ({:+.1%})</b>".format(
            broker.state["balance"], unreal, eq, eq / init - 1))

    return "<br>".join(snap)


def execute_live(sym, sig, price, sl, tp):
    """实盘执行：100U保证金x100=10000U名义，市价开仓+止损/止盈条件单。"""
    contract = sym.replace("USDT", "_USDT")
    try:
        from gate_trader import GateFutures
        trader = getattr(execute_live, "_trader", None)
        if trader is None:
            trader = GateFutures()
            execute_live._trader = trader
        order, sl_ord, tp_ord, size = trader.open_position(
            contract, sig, 10000.0, price, sl, tp)
        log_alert("[实盘成交] {} {} {}张 @市价 | 止损单#{} 止盈单#{}".format(
            contract, "做多" if sig == 1 else "做空", size,
            sl_ord.get("id"), tp_ord.get("id")))
    except Exception as e:
        log_alert("[实盘失败] {} 信号{} 未成交: {}".format(contract, sig, e))


def main():
    ap = argparse.ArgumentParser(description="MACD二次交叉形态多周期实时监控（共享模拟资金池/实盘）")
    ap.add_argument("--watch", default="",
                    help="信号流，格式 币种:周期:止损:止盈，多项逗号分隔；"
                         "例 ETHUSDT:1h:0.01:0.02,ETHUSDT:4h:0.03:0.03,BTCUSDT:4h:0.03:0.03")
    ap.add_argument("--timeframe", default="1h", choices=sorted(TF_SECONDS),
                    help="未指定--watch时的单流周期")
    ap.add_argument("--sl", type=float, default=0.01)
    ap.add_argument("--tp", type=float, default=0.02)
    ap.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT"])
    ap.add_argument("--pool", type=float, default=1000.0, help="共享模拟资金池本金(U)")
    ap.add_argument("--once", action="store_true", help="只检查一次就退出")
    mode_grp = ap.add_mutually_exclusive_group()
    mode_grp.add_argument("--paper", action="store_true", help="模拟盘（默认）")
    mode_grp.add_argument("--signal-only", action="store_true", help="仅信号提示，不模拟成交")
    mode_grp.add_argument("--live", action="store_true", help="Gate.io实盘（需gate_config.json）")
    ap.add_argument("--tag", default="", help="实例标记（隔离模拟账户文件）")
    ap.add_argument("--no-status-notify", action="store_true",
                    help="关闭每轮形态检测后的微信群状态推送（信号推送不受影响）")
    ap.add_argument("--trend-filter", type=int, default=0,
                    help="趋势过滤EMA周期（0=关闭，如200：EMA上方只做多、下方只做空）")
    args = ap.parse_args()
    args.paper = not (args.signal_only or args.live)

    streams = parse_watch(args.watch) if args.watch else \
        [{"sym": s, "tf": args.timeframe, "sl": args.sl, "tp": args.tp,
          "key": "{}|{}".format(s, args.timeframe)} for s in args.symbols]
    desc = ", ".join("{} {}({:.1%}/{:.1%})".format(c["sym"], c["tf"], c["sl"], c["tp"]) for c in streams)

    seen = set()
    if args.live:
        mode = "!!! 实盘模式：信号将真实下单 !!!"
    elif args.paper:
        mode = "模拟盘：多信号流共享 {:.0f}U 资金池".format(args.pool)
    else:
        mode = "仅信号提示"
    print("监控启动信号流: {} | {}".format(desc, mode))
    if args.trend_filter:
        print("趋势过滤: EMA{}（EMA上方只做多、下方只做空，逆势信号跳过）".format(args.trend_filter))

    broker = None
    if args.paper:
        state_file, trades_log = _file_paths(args.tag)
        broker = PaperBroker(args.pool, state_file, trades_log)
        print("模拟账户文件: {} | 成交记录: {}".format(state_file, trades_log))
    if args.live:
        try:
            from gate_trader import GateFutures
            GateFutures()
            print("Gate API 密钥已加载")
        except Exception as e:
            print("实盘模式启动失败: {}".format(e))
            sys.exit(1)

    while True:
        try:
            snapshot = check_streams(streams, args, seen, broker)
            # 每轮形态检测后向微信群推送一次状态快照
            if snapshot and not args.no_status_notify:
                try:
                    import wechat_notify
                    title = "MACD形态巡检 {}".format(dt.datetime.now().strftime("%m-%d %H:%M"))
                    wechat_notify.send(title, snapshot)
                    print("[微信群推送] {} 已发送".format(title))
                except Exception as e:
                    print("状态快照推送失败（不影响监控）: {}".format(e))
        except Exception as e:
            print("本轮检查失败（下轮重试）: {}".format(e))
        if args.once:
            break
        # 睡到所有信号流中最近的K线收盘后 8 秒
        now = time.time()
        wait = min(TF_SECONDS[c["tf"]] - (now % TF_SECONDS[c["tf"]]) for c in streams) + 8
        print("下次检查: {} ({}分钟后)\n".format(
            dt.datetime.fromtimestamp(now + wait).strftime("%H:%M:%S"), round(wait / 60, 1)))
        time.sleep(wait)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n监控已停止")
        sys.exit(0)
