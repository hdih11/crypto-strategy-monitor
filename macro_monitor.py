# -*- coding: utf-8 -*-
"""
关键宏观数据监控: 美债收益率 / 美元指数DXY / 美元兑日元USDJPY
================================================================
数据源（均为国内可直连）:
  - 美债收益率(2Y/5Y/10Y/30Y)与10Y-2Y利差: 东方财富数据中心
    日度数据, 美股收盘后更新当日（北京凌晨）, 本地缓存10分钟
  - 美元指数DXY、美元兑日元USDJPY: 新浪实时行情（盘中实时刷新）

运行:
  python macro_monitor.py               # 常驻轮询, 默认60秒一轮, Ctrl+C 停止
  python macro_monitor.py --once        # 只巡检一轮后退出
  python macro_monitor.py --interval 30 # 自定义轮询间隔(秒)
  python macro_monitor.py --no-push     # 本轮起禁用微信推送(仅终端输出)

告警(终端高亮 + 蜂鸣 + 微信PushPlus群组, 复用 wechat_config.json):
  - 美债2Y/10Y/30Y 日变动 >= yield_alert_bp(默认5bp)
  - 10Y-2Y利差日变动 >= spread_alert_bp(默认8bp)
  - DXY / USDJPY 日内涨跌幅 >= alert_pct(默认0.5%), 按阈值倍数阶梯重复告警
  - USDJPY 穿越关键整数位(默认 145/150/152/155/158/160/162/165)
  同一阶梯/关键位同一天只告警一次, 避免刷屏。
每日 08:00(北京时间)后首轮巡检自动推送前日宏观数据日报。
状态持久化 macro_state.json, 告警记录 macro_alerts.log。
"""
import argparse
import datetime as dt
import json
import os
import threading
import time

import requests

STATE_FILE = "macro_state.json"
ALERT_LOG = "macro_alerts.log"
YIELD_TTL = 600  # 美债日度数据本地缓存秒数

SINA_URL = "https://hq.sinajs.cn/list=DINIW,fx_susdjpy"
YIELD_URL = "https://datacenter.eastmoney.com/api/data/get"
YIELD_TOKEN = "894050c76af8597a853f5b408b759f5d"
# 东财字段 -> 名称（与 akshare bond_zh_us_rate 口径一致；EMG00159635日本10Y该源已停更, 不再监控）
YIELD_FIELDS = [("EMG00001306", "美债2Y"), ("EMG00001308", "美债5Y"),
                ("EMG00001310", "美债10Y"), ("EMG00001312", "美债30Y"),
                ("EMG01339436", "利差10Y-2Y")]
USDJPY_LEVELS = [145, 150, 152, 155, 158, 160, 162, 165]

UA = {"User-Agent": "Mozilla/5.0",
      "Referer": "https://finance.sina.com.cn/"}


# ---------------------------------------------------------------- 基础设施
def now_str():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def log_alert(msg):
    line = "[{}] {}".format(now_str(), msg)
    print("\n" + "!" * 60)
    print(line)
    print("!" * 60 + "\n")
    with open(ALERT_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    try:
        import winsound
        for _ in range(3):
            winsound.Beep(1200, 250)
            time.sleep(0.1)
    except Exception:
        pass


def push_wechat(title, content, push_enabled=True):
    """微信推送（线程异步, 终端回显结果; 未配置/被禁用时跳过）"""
    if not push_enabled:
        print("[微信推送] 已禁用(--no-push)")
        return
    def _run():
        try:
            import wechat_notify
            _, errs = wechat_notify.send(title, content)
            if errs:
                print("[微信推送] 失败: {}".format("; ".join(errs)))
            else:
                print("[微信推送] 已发送: {}".format(title))
        except Exception as e:
            print("[微信推送] 异常: {}".format(e))
    threading.Thread(target=_run, daemon=True).start()


# ---------------------------------------------------------------- 数据抓取
def fetch_fx():
    """新浪实时: 美元指数DINIW + 美元兑日元fx_susdjpy
    字段: 0时间 1最新 2买价 3昨收 4时间戳 5开盘 6最高 7最低 8最新 名称 [10涨跌幅 11涨跌额] ... 日期"""
    r = requests.get(SINA_URL, headers=UA, timeout=8)
    r.encoding = "gb18030"
    out = {}
    for line in r.text.splitlines():
        if "=" not in line or '"' not in line:
            continue
        tag = line.split("_")[-1].split("=")[0].strip()
        raw = line.split('"')[1]
        if not raw:
            continue
        p = raw.split(",")
        try:
            item = {"time": p[0], "last": float(p[1]), "prev_close": float(p[3]),
                    "open": float(p[5]), "high": float(p[6]), "low": float(p[7]),
                    "date": p[-1], "name": p[9]}
            if tag == "fx_susdjpy" and len(p) > 11:  # 汇率品种自带涨跌幅字段
                item["pct"] = float(p[10])
        except (ValueError, IndexError):
            continue
        if item["last"] > 0:
            out["DXY" if tag == "DINIW" else "USDJPY"] = item
    return out


def fetch_yields(ps=15):
    """东财数据中心日度收益率, 返回按日期升序的 [{date, 各期限}]"""
    r = requests.get(YIELD_URL, params={"type": "RPTA_WEB_TREASURYYIELD", "sty": "ALL",
                                        "st": "SOLAR_DATE", "sr": "-1",
                                        "token": YIELD_TOKEN, "p": 1, "ps": ps},
                     headers=UA, timeout=10)
    rows = r.json()["result"]["data"]
    out = []
    for row in rows:
        d = row["SOLAR_DATE"][:10]
        item = {"date": d}
        for code, name in YIELD_FIELDS:
            try:
                v = row.get(code)
                item[name] = float(v) if v is not None else None
            except (TypeError, ValueError):
                item[name] = None
        out.append(item)
    return sorted(out, key=lambda x: x["date"])


# ---------------------------------------------------------------- 巡检逻辑
def yield_alerts(rows, cfg, state):
    """美债日变动告警: 每个期限各自取最近两个有效值比较(当日行可能缺数/日本10Y滞后)"""
    alerts = []
    if len(rows) < 2:
        return alerts
    steps = state.setdefault("yield_steps", {})
    for name, thr in [("美债2Y", cfg["yield_alert_bp"]), ("美债10Y", cfg["yield_alert_bp"]),
                      ("美债30Y", cfg["yield_alert_bp"]),
                      ("利差10Y-2Y", cfg["spread_alert_bp"])]:
        vals = [(r["date"], r[name]) for r in rows if r.get(name) is not None]
        if len(vals) < 2:
            continue
        (pd_, pv), (cd_, cv) = vals[-2], vals[-1]
        chg_bp = round((cv - pv) * 100, 1)
        if abs(chg_bp) < thr:
            continue
        day_steps = steps.setdefault(cd_, {})
        mult = int(abs(chg_bp) // thr)
        if mult <= day_steps.get(name, 0):
            continue  # 该阶梯已告警过
        day_steps[name] = mult
        arrow = "↑" if chg_bp > 0 else "↓"
        alerts.append("{}({}) {} 日变动 {:+.1f}bp ({} {:.3f}% -> {:.3f}%)".format(
            name, cd_, arrow, chg_bp, arrow, pv, cv))
    return alerts


def fx_alerts(sym, item, cfg, state):
    """汇率日内涨跌幅阶梯告警 + USDJPY关键位穿越告警"""
    alerts = []
    fx_state = state.setdefault("fx", {})
    s = fx_state.setdefault(sym, {"date": item["date"], "steps": 0})
    if s["date"] != item["date"]:  # 新交易日, 重置阶梯
        s.update({"date": item["date"], "steps": 0})
    pct = item.get("pct")
    if pct is None:
        if item["prev_close"] > 0 and abs(item["last"] / item["prev_close"] - 1) < 0.05:
            pct = (item["last"] / item["prev_close"] - 1) * 100
        else:
            pct = 0.0
    thr = cfg["alert_pct"]
    mult = int(abs(pct) // thr)
    if mult > s["steps"]:
        s["steps"] = mult
        arrow = "↑" if pct > 0 else "↓"
        alerts.append("{} {} 日内涨跌 {:+.2f}% ({:.4f})".format(sym, arrow, pct, item["last"]))
    if sym == "USDJPY":
        hits = state.setdefault("level_hits", {})
        day_hits = hits.setdefault(item["date"], [])
        prev_c, last = item["prev_close"], item["last"]
        for lv in USDJPY_LEVELS:
            crossed = (prev_c < lv <= last) or (last <= lv < prev_c)
            if not crossed or lv in day_hits:
                continue
            day_hits.append(lv)
            direction = "上穿" if last > prev_c else "下破"
            alerts.append("USDJPY {}关键位 {} (现 {:.2f})".format(direction, lv, last))
    return alerts


def build_yield_lines(rows):
    """美债巡检展示行: 每个期限取最新有效值+相对上一有效值的日变动(各自日期可不同)"""
    if not rows:
        return ["  美债收益率: 暂无数据"], None
    base = next((r for r in reversed(rows) if r["美债10Y"] is not None), rows[-1])
    parts = []
    for _, name in YIELD_FIELDS:
        vals = [(r["date"], r[name]) for r in rows if r.get(name) is not None]
        if not vals:
            parts.append("{} N/A".format(name))
            continue
        (dv, v) = vals[-1]
        pd_, pv = vals[-2] if len(vals) > 1 else (None, None)
        line = "{:.3f}".format(v)
        if pv is not None:
            line += " ({:+.1f}bp)".format((v - pv) * 100)
        if dv != base["date"]:
            line += "[{}]".format(dv[5:])
        parts.append(line)
    main = "  美债/日债({}): {}".format(base["date"], " | ".join(parts))
    return [main], base


def daily_summary(yields, fx, cfg):
    """每日日报文案"""
    lines = ["<b>宏观数据日报 {}</b>".format(now_str()[:10]), ""]
    if yields:
        base = next((r for r in reversed(yields) if r["美债10Y"] is not None), None)
        if base is not None:
            lines.append("<b>美债/日债收益率 ({})</b>".format(base["date"]))
            for _, name in YIELD_FIELDS:
                vals = [(r["date"], r[name]) for r in yields if r.get(name) is not None]
                if not vals:
                    continue
                (dv, v) = vals[-1]
                bp = ""
                if len(vals) > 1:
                    bp = " ({:+.1f}bp)".format((v - vals[-2][1]) * 100)
                extra = "[{}]".format(dv[5:]) if dv != base["date"] else ""
                lines.append("· {}: {:.3f}{}{}".format(name, v, bp, extra))
            lines.append("")
    if fx:
        lines.append("<b>实时汇率</b>")
        for sym in ("DXY", "USDJPY"):
            it = fx.get(sym)
            if not it:
                continue
            pct = it.get("pct")
            if pct is None and it["prev_close"] > 0:
                pct = (it["last"] / it["prev_close"] - 1) * 100
            lines.append("· {}({}): {:.4f} 日内 {:+.2f}%".format(
                sym, it["time"], it["last"], pct if pct is not None else 0.0))
    return "<br/>".join(lines)


# ---------------------------------------------------------------- 主循环
def one_round(state, cfg, push_enabled, first_run=False):
    round_no = state.get("round", 0) + 1
    state["round"] = round_no
    print("=" * 72)
    print("[{}] 巡检 #{}".format(now_str(), round_no))

    # ---- 汇率(实时)
    try:
        fx = fetch_fx()
    except Exception as e:
        fx = {}
        print("  [警告] 新浪汇率抓取失败: {}".format(e))
    for sym in ("DXY", "USDJPY"):
        it = fx.get(sym)
        if not it:
            print("  {} 暂无数据".format(sym))
            continue
        pct = it.get("pct")
        if pct is None and it["prev_close"] > 0:
            pct = (it["last"] / it["prev_close"] - 1) * 100
        print("  {}({})  {:.4f}  日内 {:+.2f}%  开{:.4f} 高{:.4f} 低{:.4f}".format(
            sym, it["time"], it["last"],
            pct if pct is not None else 0.0, it["open"], it["high"], it["low"]))

    # ---- 美债/日债(日度, 带缓存)
    yields, y_err = [], None
    ts, cache = state.get("yield_cache", [0, []])
    if time.time() - ts < YIELD_TTL and cache:
        yields = cache
    else:
        try:
            yields = fetch_yields()
            state["yield_cache"] = [time.time(), yields]
        except Exception as e:
            y_err = str(e)
            yields = cache
            print("  [警告] 美债数据抓取失败: {}".format(e))
    yield_lines, cur_row = build_yield_lines(yields)
    for ln in yield_lines:
        print(ln)

    # ---- 告警判定(首轮只建档不告警, 避免重启刷屏)
    alerts = []
    if not first_run:
        alerts += yield_alerts(yields, cfg, state)
        for sym in ("DXY", "USDJPY"):
            if fx.get(sym):
                alerts += fx_alerts(sym, fx[sym], cfg, state)
    else:
        yield_alerts(yields, cfg, state)  # 建档
        for sym in ("DXY", "USDJPY"):
            if fx.get(sym):
                fx_alerts(sym, fx[sym], cfg, state)

    # ---- 每日日报推送
    today = now_str()[:10]
    if state.get("last_summary") != today and not first_run:
        hour_ok = dt.datetime.now().hour >= cfg["summary_hour"]
        if hour_ok and (fx or yields):
            push_wechat("宏观数据日报", daily_summary(yields, fx, cfg), push_enabled)
            state["last_summary"] = today

    save_state(state)

    # ---- 告警输出
    if alerts:
        push_wechat("宏观数据监控提醒", "<br/>".join(alerts), push_enabled)
        for a in alerts:
            log_alert(a)
    nxt = dt.datetime.now() + dt.timedelta(seconds=cfg["interval"])
    print("  下轮检查 {}:{}".format(nxt.strftime("%H:%M:%S"),
                                   " (首轮建档, 不告警)" if first_run else ""))
    return y_err


def main():
    ap = argparse.ArgumentParser(description="美债/DXY/USDJPY 宏观数据监控")
    ap.add_argument("--interval", type=int, default=60, help="轮询间隔秒数, 默认60")
    ap.add_argument("--once", action="store_true", help="只巡检一轮后退出")
    ap.add_argument("--no-push", action="store_true", help="禁用微信推送")
    args = ap.parse_args()

    cfg = {"interval": args.interval, "yield_alert_bp": 5, "spread_alert_bp": 8,
           "alert_pct": 0.5, "summary_hour": 8}
    state = load_state()
    state.pop("yield_cache", None)  # 重启后强制刷新美债缓存

    print("关键宏观数据监控启动 [{}]".format(now_str()))
    print("  数据源: 美债/日债=东财数据中心(日度,缓存{}s) DXY/USDJPY=新浪实时".format(YIELD_TTL))
    print("  轮询间隔 {}s | 告警阈值: 美债{}bp 利差{}bp 汇率{:.1f}%".format(
        cfg["interval"], cfg["yield_alert_bp"], cfg["spread_alert_bp"], cfg["alert_pct"]))
    print("  USDJPY关键位: {} | 微信推送: {}".format(
        "/".join(str(x) for x in USDJPY_LEVELS), "关(--no-push)" if args.no_push else "开"))

    first = "first_run" not in state
    state["first_run"] = False
    one_round(state, cfg, not args.no_push, first_run=first)
    if args.once:
        return
    while True:
        time.sleep(cfg["interval"])
        try:
            one_round(state, cfg, not args.no_push)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print("  [异常] 本轮巡检失败, 下轮继续: {}".format(repr(e)))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n监控已停止 [{}]".format(now_str()))
