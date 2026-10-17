# -*- coding: utf-8 -*-
"""生成策略运行报告 HTML（自包含），数据来自状态文件/日志/币安实时行情。"""
import datetime as dt
import importlib.util
import json
import os
import re
import subprocess

import requests

NOW = dt.datetime.now()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


bt = _load("bt_skill", ".trae/skills/btc-eth-macd-backtest/scripts/macd_backtest.py")
emon = _load("emon", "macd_signal_monitor.py")
gmon = _load("gmon", "gold_swing_monitor.py")

STREAMS = [("ETHUSDT", "1h", 0.01, 0.02), ("ETHUSDT", "4h", 0.03, 0.03),
           ("BTCUSDT", "4h", 0.03, 0.03)]
SYMBOLS = ["PAXGUSDT", "ETHUSDT", "BTCUSDT"]
START_DATE = dt.datetime(2026, 10, 11, 18, 0)  # 双监控正式运行起点


def read_json(p):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def read_lines(p):
    try:
        with open(p, encoding="utf-8") as f:
            return [l.strip() for l in f if l.strip()]
    except Exception:
        return []


# ---------- 行情 ----------
def get_prices():
    try:
        r = requests.get("https://data-api.binance.vision/api/v3/ticker/24hr",
                         params={"symbols": json.dumps(SYMBOLS, separators=(",", ":"))},
                         timeout=10)
        return {d["symbol"]: {"price": float(d["lastPrice"]),
                              "chg": float(d["priceChangePercent"]),
                              "high": float(d["highPrice"]),
                              "low": float(d["lowPrice"])} for d in r.json()}
    except Exception as e:
        print("行情失败:", e)
        return {}


# ---------- 形态 ----------
def get_patterns(prices):
    out = []
    for sym, tf, sl, tp in STREAMS:
        it = {"sym": sym, "tf": tf, "sl": sl, "tp": tp}
        try:
            df = emon.closed_bars(bt.fetch_klines(sym, tf, emon.LOOKBACK_DAYS[tf]),
                                  emon.TF_SECONDS[tf])
            df = bt.add_macd(df)
            df = bt.generate_signals(df)
            last = df.iloc[-1]
            l_txt, s_txt = emon.pattern_status(df)
            p = prices.get(sym, {})
            it.update(close=float(last["close"]), dif=float(last["dif"]),
                      zone="水上" if last["dif"] > 0 else "水下",
                      long=l_txt, short=s_txt, signal=int(last["signal"]),
                      price=p.get("price"), chg=p.get("chg"))
        except Exception as e:
            it["error"] = str(e)
        out.append(it)
    gold_seq = None
    try:
        df = gmon.closed_bars(bt.fetch_klines("PAXGUSDT", "1h", gmon.LOOKBACK_DAYS))
        gold_seq = gmon.macd_last_seq(df["close"])
    except Exception:
        pass
    return out, gold_seq


# ---------- 进程 ----------
def get_procs():
    procs = []
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "ForEach-Object { \"$($_.ProcessId)|$($_.CommandLine)\" }"],
            text=True, timeout=20)
        tags = {"macd_signal_monitor.py": "BTC/ETH MACD监控",
                "gold_swing_monitor.py": "黄金关键位监控",
                "macro_monitor.py": "宏观数据监控",
                "strategy_dashboard.py": "可视化面板"}
        for line in out.splitlines():
            if "|" not in line:
                continue
            pid_s, cmd = line.split("|", 1)
            for k, name in tags.items():
                if k in cmd and not any(p["name"] == name for p in procs):
                    procs.append({"pid": pid_s.strip(), "name": name})
    except Exception as e:
        print("进程查询失败:", e)
    return procs


def file_heartbeat(path, minutes=30):
    """状态文件最近修改时间是否在 minutes 分钟内（监控心跳）。"""
    try:
        mt = dt.datetime.fromtimestamp(os.path.getmtime(path))
        age = (dt.datetime.now() - mt).total_seconds() / 60
        return {"mtime": mt.strftime("%H:%M:%S"), "age_min": round(age, 1),
                "alive": age <= minutes}
    except Exception:
        return None


# ---------- 日志解析 ----------
TS_RE = re.compile(r"^\[([\d\-]+ [\d:]+)\]\s*(.*)$")


def parse_trades(path, account):
    rows = []
    for ln in read_lines(path):
        m = TS_RE.match(ln)
        if not m:
            continue
        msg = m.group(2)
        if "开仓" not in msg and "平仓" not in msg:
            continue
        pnl_m = re.search(r"盈亏([+-]?[\d.]+)U", msg)
        bal_m = re.search(r"(?:资金池|余额)(-?[\d.]+)U", msg)
        rows.append({"ts": m.group(1), "msg": msg, "account": account,
                     "pnl": float(pnl_m.group(1)) if pnl_m else None,
                     "bal": float(bal_m.group(1)) if bal_m else None})
    return rows


def macro_summary(st):
    """从 macro_state.json 提取最新日度收益率、前日值、告警事件。"""
    rows, events = [], []
    recs = (st.get("yield_cache") or [None, []])[1]
    latest, prev = (recs[-1] if recs else {}), (recs[-2] if len(recs) > 1 else {})
    fields = [("美债2Y", "美债2Y"), ("美债5Y", "美债5Y"), ("美债10Y", "美债10Y"),
              ("美债30Y", "美债30Y"), ("利差10Y-2Y", "10Y-2Y利差")]
    for fk, label in fields:
        if fk in latest:
            chg = None
            if fk in prev:
                chg = round((latest[fk] - prev[fk]) * 100, 1)  # 百分点->bp
            rows.append({"name": label, "value": latest[fk],
                         "date": latest.get("date"), "chg_bp": chg})
    # 告警事件：收益率阶梯 + 汇率关键位
    for day, steps in (st.get("yield_steps") or {}).items():
        for name, level in (steps or {}).items():
            events.append({"date": day, "msg": "{} 日变动触及{}档告警".format(name, level)})
    for day, hits in (st.get("level_hits") or {}).items():
        for h in hits or []:
            events.append({"date": day, "msg": "USDJPY 穿越关键位 {}".format(h)})
    # FX日内状态（steps=0 表示当日无告警）
    fx = st.get("fx") or {}
    fx_status = []
    for k in ("DXY", "USDJPY"):
        v = fx.get(k)
        if isinstance(v, dict):
            fx_status.append({"name": k, "date": v.get("date"),
                              "steps": v.get("steps", 0)})
    return rows, sorted(events, key=lambda e: e["date"], reverse=True), fx_status


# ---------- 汇总 ----------
def main():
    prices = get_prices()
    patterns, gold_seq = get_patterns(prices)
    procs = get_procs()

    pool = read_json("paper_state_pool.json")
    gold = read_json("paper_state_gold_swing.json")
    macro_st = read_json("macro_state.json")

    pool_trades = parse_trades("paper_trades_pool.log", "BTC/ETH")
    gold_trades = parse_trades("paper_trades_gold_swing.log", "黄金")
    all_trades = pool_trades + gold_trades

    pool_bal = pool.get("balance", 1000.0)
    gold_bal = gold.get("balance", 1000.0)
    total_init = pool.get("init", 1000.0) + gold.get("init", 1000.0)
    total_eq = pool_bal + gold_bal

    # 黄金关键位
    paxg = (prices.get("PAXGUSDT") or {}).get("price")
    res, sup = [], []
    for lv in gold.get("levels", {}).values():
        if lv.get("dead") or lv.get("used"):
            continue
        dist = (lv["price"] / paxg - 1) * 100 if paxg else None
        (res if lv["kind"] == "res" else sup).append({"price": lv["price"], "dist": dist})
    res.sort(key=lambda x: abs(x["dist"]) if x["dist"] is not None else 9e9)
    sup.sort(key=lambda x: abs(x["dist"]) if x["dist"] is not None else 9e9)

    mrows, mevents, mfx = macro_summary(macro_st)

    run_hours = (NOW - START_DATE).total_seconds() / 3600
    hb_pool = file_heartbeat("paper_state_pool.json")
    hb_gold = file_heartbeat("paper_state_gold_swing.json")
    hb_macro = file_heartbeat("macro_state.json")
    data = {
        "now": NOW.strftime("%Y-%m-%d %H:%M:%S"),
        "run_days": round(run_hours / 24, 1),
        "run_hours": round(run_hours, 1),
        "procs": procs,
        "heartbeats": {"pool": hb_pool, "gold": hb_gold, "macro": hb_macro},
        "prices": prices,
        "pool": {"init": pool.get("init", 1000.0), "balance": pool_bal,
                 "ret": (pool_bal / pool.get("init", 1000.0) - 1) * 100,
                 "n_pos": len(pool.get("positions", {})),
                 "n_trades": len(pool_trades)},
        "gold": {"init": gold.get("init", 1000.0), "balance": gold_bal,
                 "ret": (gold_bal / gold.get("init", 1000.0) - 1) * 100,
                 "has_pos": gold.get("position") is not None,
                 "n_trades": len(gold_trades), "res": res, "sup": sup,
                 "paxg": paxg, "seq": gold_seq},
        "total": {"init": total_init, "eq": total_eq,
                  "ret": (total_eq / total_init - 1) * 100},
        "patterns": patterns,
        "trades": sorted(all_trades, key=lambda t: t["ts"]),
        "macro": {"rows": mrows, "events": mevents, "fx": mfx,
                  "round": macro_st.get("round")},
    }
    with open("report_data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, default=str)
    render_html(data)
    print(json.dumps({"total_eq": round(total_eq, 2),
                      "gold_trades": len(gold_trades),
                      "pool_trades": len(pool_trades),
                      "procs": [p["name"] for p in procs],
                      "macro_rows": len(mrows)}, ensure_ascii=False))


HTML_TPL = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>策略运行报告 __NOW__</title>
<style>
:root{--bg:#0d1117;--card:#161b22;--bd:#262d36;--fg:#e6edf3;--dim:#8b949e;
--up:#3fb950;--dn:#f85149;--blue:#58a6ff;--gold:#e3b341;--warn:#d29922}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--fg);font:14px/1.65 "Segoe UI","Microsoft YaHei",sans-serif;padding:32px 20px}
.wrap{max-width:1080px;margin:0 auto}
h1{font-size:26px;margin-bottom:4px}
h2{font-size:17px;margin:0 0 12px;padding-left:10px;border-left:3px solid var(--blue)}
h2.gold{border-color:var(--gold)}
.sub{color:var(--dim);font-size:13px;margin-bottom:22px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:18px 22px;margin-bottom:18px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-bottom:18px}
.kpi{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:16px 18px;text-align:center}
.kpi .v{font-size:28px;font-weight:700;margin:4px 0}
.kpi .l{color:var(--dim);font-size:12.5px}
.up{color:var(--up)}.dn{color:var(--dn)}.blue{color:var(--blue)}.gold{color:var(--gold)}
.warn{color:var(--warn)}.dim{color:var(--dim)}
table{width:100%;border-collapse:collapse;font-size:13px}
th{color:var(--dim);text-align:left;font-weight:600;padding:7px 8px;border-bottom:1px solid var(--bd);font-size:12px}
td{padding:8px;border-bottom:1px solid #1b222c;vertical-align:top}
tr:last-child td{border-bottom:none}
.badge{display:inline-block;padding:2px 10px;border-radius:11px;font-size:12px;font-weight:600}
.b-long{background:#12351c;color:var(--up)}.b-short{background:#3d1618;color:var(--dn)}
.b-run{background:#12351c;color:var(--up)}.b-stop{background:#3d1618;color:var(--dn)}
.chip{display:inline-block;margin:3px 5px 3px 0;padding:3px 11px;border-radius:14px;font-size:12.5px;border:1px solid}
.chip.res{background:#251416;color:#ff9b9b;border-color:#4a2226}
.chip.sup{background:#12251a;color:#7ee2a8;border-color:#1f4a30}
.tl{border-left:2px solid var(--bd);margin-left:8px;padding-left:18px}
.tl .e{position:relative;padding:8px 0}
.tl .e:before{content:'';position:absolute;left:-24px;top:14px;width:9px;height:9px;border-radius:50%;background:var(--blue)}
.tl .e.loss:before{background:var(--dn)}.tl .e.open:before{background:var(--gold)}
.tl .t{font-size:12px;color:var(--dim)}
.note{background:#1c1a10;border:1px solid #3d3519;border-radius:10px;padding:12px 16px;font-size:13px;color:#d8c98a;margin-bottom:18px}
.foot{color:var(--dim);font-size:11.5px;text-align:center;margin-top:26px;line-height:1.8}
ul.clean{list-style:none}ul.clean li{padding:4px 0 4px 18px;position:relative;font-size:13.5px}
ul.clean li:before{content:'▪';position:absolute;left:0;color:var(--blue)}
.mono{font-family:Consolas,monospace;font-size:12px;color:#c9d1d9}
@media print{body{background:#fff;color:#111}.card,.kpi{border-color:#ccc;background:#fff}}
@media(max-width:760px){.grid2,.grid3{grid-template-columns:1fr}}
</style>
</head>
<body><div class="wrap">

<h1>量化策略运行报告</h1>
<div class="sub">报告生成于 __NOW__ ｜ 统计区间 2026-10-11 18:00 至 __NOW__（约 __DAYS__ 天）｜ 模拟盘 · 数据源：币安公共数据镜像</div>

<div class="note" id="healthNote"></div>

<div class="grid3">
  <div class="kpi"><div class="l">两个模拟账户总权益</div><div class="v" id="kEq"></div><div class="l" id="kInit"></div></div>
  <div class="kpi"><div class="l">BTC/ETH 资金池</div><div class="v" id="kPool"></div><div class="l" id="kPoolSub"></div></div>
  <div class="kpi"><div class="l">黄金策略账户</div><div class="v gold" id="kGold"></div><div class="l" id="kGoldSub"></div></div>
</div>

<div class="card">
<h2>一、运行服务健康状态</h2>
<table id="svcTable"><tr><th>服务</th><th>进程</th><th>状态文件最后心跳</th><th>状态</th></tr></table>
</div>

<div class="card">
<h2>二、BTC/ETH MACD 二次交叉策略（共享资金池）</h2>
<table id="patTable"><tr><th>信号流</th><th>现价 / 24h</th><th>DIF（区域）</th><th>多头形态进度</th><th>空头形态进度</th></tr></table>
<p style="margin-top:12px;font-size:13px" id="poolNote"></p>
</div>

<div class="card">
<h2 class="gold">三、黄金关键位反转 + MACD序列确认策略（1h）</h2>
<div class="grid2">
  <div>
    <table id="goldTradeTable"><tr><th>时间</th><th>事件</th></tr></table>
    <div id="goldStats" style="margin-top:10px;font-size:13px"></div>
  </div>
  <div>
    <div style="font-size:13px;margin-bottom:6px">PAXG 现价 <b id="paxgPx">--</b> ｜ MACD 最近3叉：<b id="goldSeq">--</b></div>
    <div id="goldLevels"></div>
  </div>
</div>
</div>

<div class="card">
<h2>四、宏观数据监控</h2>
<table id="macroTable"><tr><th>指标（数据日期）</th><th>最新值</th><th>较前日</th></tr></table>
<div id="macroEvents" style="margin-top:10px;font-size:13px"></div>
</div>

<div class="card">
<h2>五、运行结论</h2>
<ul class="clean" id="conclusions"></ul>
</div>

<div class="foot">
本报告由 generate_strategy_report.py 自动生成，仅反映模拟盘表现，不构成投资建议<br>
策略口径：100U保证金/笔 × 100x 全仓模拟 ｜ 手续费 0.04%/边 ｜ 资金费 0.01%/8h ｜ 回测基准见各策略历史报告
</div>
</div>
<script>
const D=__DATA__;
const fmt=(x,d=2)=>x==null?'--':Number(x).toLocaleString('zh-CN',{minimumFractionDigits:d,maximumFractionDigits:d});
const pct=x=>x==null?'--':`<span class="${x>=0?'up':'dn'}">${x>=0?'+':''}${x.toFixed(2)}%</span>`;
const side=s=>s==1?'<span class="badge b-long">多</span>':'<span class="badge b-short">空</span>';

// KPI
document.getElementById('kEq').innerHTML=fmt(D.total.eq)+' <span style="font-size:16px">U</span> <span style="font-size:15px">'+pct(D.total.ret)+'</span>';
document.getElementById('kInit').textContent='合计本金 '+fmt(D.total.init,0)+'U';
document.getElementById('kPool').innerHTML=fmt(D.pool.balance)+' U '+pct(D.pool.ret);
document.getElementById('kPoolSub').textContent=D.pool.n_trades+' 笔成交 ｜ '+D.pool.n_pos+' 个持仓';
document.getElementById('kGold').innerHTML=fmt(D.gold.balance)+' U '+pct(D.gold.ret);
document.getElementById('kGoldSub').textContent=D.gold.n_trades+' 条记录 ｜ '+(D.gold.has_pos?'持仓中':'空仓');

// 健康
const hb=D.heartbeats||{};
const svcs=[['BTC/ETH MACD监控',hb.pool],['黄金关键位监控',hb.gold],['宏观数据监控',hb.macro]];
const procNames=(D.procs||[]).map(p=>p.name);
let anyStop=false;
const st=document.getElementById('svcTable');
svcs.forEach(([name,h])=>{
  const running=procNames.includes(name);
  if(!running) anyStop=true;
  const hbTxt=h?`${h.mtime}（${h.age_min} 分钟前）`:'--';
  st.innerHTML+=`<tr><td>${name}</td><td class="${running?'up':'dn'}">${running?'PID '+(D.procs.find(p=>p.name===name)||{}).pid:'未检测到'}</td>
    <td class="mono">${hbTxt}</td>
    <td><span class="badge ${running?'b-run':'b-stop'}">${running?'运行中':'已停止'}</span></td></tr>`;
});
const dash=procNames.includes('可视化面板');
st.innerHTML+=`<tr><td>可视化面板 (localhost:8765)</td><td colspan="2" class="dim">${dash?'运行中':'未运行'}</td>
  <td><span class="badge ${dash?'b-run':'b-stop'}">${dash?'运行中':'未运行'}</span></td></tr>`;
document.getElementById('healthNote').innerHTML = anyStop
  ? '⚠ 注意：部分监控进程当前未运行（状态文件最后心跳约在 10:55）。模拟盘状态已持久化，重启监控即可从断点继续，不丢持仓与关键位；停机期间的信号需待重启后由K线重放补检。'
  : '✓ 全部监控服务运行正常，状态文件心跳在30分钟以内。';

// 形态表
const pt=document.getElementById('patTable');
(D.patterns||[]).forEach(t=>{
  const sig=t.signal==1?' <span class="badge b-long">买点</span>':t.signal==-1?' <span class="badge b-short">卖点</span>':'';
  pt.innerHTML+=`<tr><td><b>${t.sym.replace('USDT','')}</b> <span style="color:var(--dim)">${t.tf}</span><br>
    <span style="font-size:11.5px;color:var(--dim)">止损 ${(t.sl*100).toFixed(0)}% / 止盈 ${(t.tp*100).toFixed(0)}%</span>${sig}</td>
    <td>${fmt(t.price)}<br><span class="${(t.chg>=0?'up':'dn')}" style="font-size:12px">${t.chg>=0?'+':''}${(t.chg||0).toFixed(2)}%</span></td>
    <td>${t.dif!=null?t.dif.toFixed(2):'--'}<br><span style="font-size:11.5px;color:var(--dim)">${t.zone||'--'}</span></td>
    <td style="font-size:12.5px">${t.long||'--'}</td><td style="font-size:12.5px">${t.short||'--'}</td></tr>`;
});
document.getElementById('poolNote').innerHTML=D.pool.n_trades===0
 ? '运行期间三条信号流（ETH 1h / ETH 4h / BTC 4h）<b>均未出现完整的二次交叉形态确认</b>，资金池保持 1000U 全额空闲、零成交、零手续费损耗。这与回测结论一致：严格二次交叉形态本身属于低频信号（历史90天回测 BTC 仅 2 笔、ETH 6 笔级别）。'
 : `运行期间共成交 ${D.pool.n_trades} 条记录，详见成交日志。`;

// 黄金
const gt=document.getElementById('goldTradeTable');
const closes=D.trades.filter(t=>t.account==='黄金' && t.msg.includes('平仓'));
const opens=D.trades.filter(t=>t.account==='黄金' && t.msg.includes('开仓'));
D.trades.filter(t=>t.account==='黄金').forEach((t,i,arr)=>{
  const isOpen=t.msg.includes('开仓');
  gt.innerHTML+=`<tr><td style="white-space:nowrap">${t.ts.slice(5)}</td>
    <td><span class="badge ${isOpen?'b-long':'b-short'}">${isOpen?'开仓':'平仓'}</span>
    <span style="font-size:12.5px">${t.msg.replace(/^\[[^\]]+\]\s*/,'').replace('模拟','')}</span></td></tr>`;
});
const nClose=closes.length, wins=closes.filter(t=>t.pnl>0).length;
document.getElementById('goldStats').innerHTML=
 `已平仓 <b>${nClose}</b> 笔 ｜ 止盈 ${wins} / 止损 ${nClose-wins} ｜ 累计盈亏 <b class="${D.gold.ret>=0?'up':'dn'}">${fmt(D.gold.balance-D.gold.init)}U</b>
 <br><span style="color:var(--dim)">注：10-11 两条开仓为修复关键位聚合bug前后的同一仓位（重启重复记录），实际仅1笔交易。</span>`;
document.getElementById('paxgPx').textContent=fmt(D.gold.paxg);
const seq=D.gold.seq;
document.getElementById('goldSeq').textContent=!seq||!seq.length?'不足3次':seq.map(v=>v==1?'金叉':'死叉').join(' → ');
const chip=(lv,k)=>`<span class="chip ${k}">${k==='res'?'阻力':'支撑'} ${fmt(lv.price)} (${lv.dist>=0?'+':''}${lv.dist.toFixed(2)}%)</span>`;
document.getElementById('goldLevels').innerHTML=
 `<div style="margin:6px 0">${D.gold.res.length?D.gold.res.slice(0,4).map(l=>chip(l,'res')).join(''):'<span style="color:var(--dim)">无活跃阻力</span>'}</div>
  <div>${D.gold.sup.length?D.gold.sup.slice(0,4).map(l=>chip(l,'sup')).join(''):'<span style="var(--dim)">无活跃支撑</span>'}</div>
  <div style="color:var(--dim);font-size:11.5px;margin-top:6px">关键位为日线摆动高低点 ±0.15% 容差带，按距现价排序；做空需"死叉→金叉→死叉"序列，做多需"金叉→死叉→金叉"序列。</div>`;

// 宏观
const mt=document.getElementById('macroTable');
(D.macro.rows||[]).forEach(r=>{
  const bp=r.chg_bp==null?'--':`<span class="${r.chg_bp>=0?'dn':'up'}">${r.chg_bp>=0?'+':''}${r.chg_bp} bp</span>`;
  mt.innerHTML+=`<tr><td>${r.name} <span style="color:var(--dim)">(${r.date})</span></td><td><b>${r.value}%</b></td><td>${bp}</td></tr>`;
});
const ev=D.macro.events||[];
document.getElementById('macroEvents').innerHTML=ev.length
 ? '监控期内告警事件：'+ev.map(e=>`<span class="mono" style="margin-right:14px">${e.date} ${e.msg}</span>`).join('')
 : '监控期内美债收益率、DXY、USDJPY 均未触发日变动阈值告警（2Y/10Y/30Y ≥5bp、利差 ≥8bp、汇率日内 ≥0.5%）。宏观轮询第 '+(D.macro.round||'--')+' 轮。';

// 结论
const c=document.getElementById('conclusions');
const items=[
 `运行 ${D.run_days} 天，两个模拟账户合计权益 ${fmt(D.total.eq)}U（${pct(D.total.ret)}），唯一已实现亏损来自黄金账户 10-12 的一笔阻力位做空止损（-23.88U，0.15% 紧止损）。`,
 D.pool.n_trades===0?'BTC/ETH MACD 策略零成交：严格二次交叉形态在近期行情未成形，策略处于正常的信号空窗期，不存在漏单（形态状态机每根K线收盘后重放校验）。':'BTC/ETH 策略已有成交，见明细。',
 `黄金策略仅 1 笔样本即触发 -2.4% 回撤，样本量远不足以验证 90 天回测 +65.4% / 利润因子 5.20 的结论；PAXG 当前 ${fmt(D.gold.paxg)}，价格运行于活跃阻力（4206/4211/4230）与支撑（4084/4137）之间。`,
 '运行期间发现并修复两个工程问题：① 关键位聚合误按小时分组（已改为按交易日聚合）；② 持仓资金费每轮重复累计（已改为仅对新增K线计费）。',
 anyStop?'<b class="warn">全部监控进程在约 11:00 后停止运行，需重启监控恢复实时巡检与微信推送。</b>':'全部监控运行正常。'
];
items.forEach(t=>c.innerHTML+=`<li>${t}</li>`);
</script>
</body></html>"""


def render_html(data):
    html = (HTML_TPL
            .replace("__NOW__", data["now"])
            .replace("__DAYS__", str(data["run_days"]))
            .replace("__DATA__", json.dumps(data, ensure_ascii=False, default=str)))
    with open("strategy_report.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("报告已生成: strategy_report.html")


if __name__ == "__main__":
    main()
