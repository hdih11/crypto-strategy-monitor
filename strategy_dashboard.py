# -*- coding: utf-8 -*-
"""
策略监控可视化面板
==================
浏览器打开 http://localhost:8765
数据来源: 两个模拟盘状态文件 + 币安实时行情 + 成交/信号日志, 页面20秒自动刷新
行情/形态计算带缓存(行情10s, 形态60s), 不给监控和交易所接口增加压力
Ctrl+C 停止
"""
import datetime as dt
import importlib.util
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

PORT = 8765
PRICE_TTL, PATTERN_TTL = 10, 60


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bt = _load("bt_skill", ".trae/skills/btc-eth-macd-backtest/scripts/macd_backtest.py")
ethbtc_mon = _load("ethbtc_mon", "macd_signal_monitor.py")
gold_mon = _load("gold_mon", "gold_swing_monitor.py")

# 与运行中监控一致的信号流配置
STREAMS = [("ETHUSDT", "1h", 0.01, 0.02),
           ("ETHUSDT", "4h", 0.03, 0.03),
           ("BTCUSDT", "4h", 0.03, 0.03)]
SYMBOLS = ["PAXGUSDT", "ETHUSDT", "BTCUSDT"]
NOTIONAL, FEE_RATE = bt.NOTIONAL, bt.FEE_RATE

_cache = {"prices": (0, None), "patterns": (0, None)}

TS_RE = re.compile(r"^\[([^\]]+)\]\s*(.*)$")
BAL_RE = re.compile(r"(?:资金池|余额)(-?[\d.]+)U")
PNL_RE = re.compile(r"盈亏([+-]?[\d.]+)U")


def get_prices():
    ts, val = _cache["prices"]
    if val is not None and time.time() - ts < PRICE_TTL:
        return val
    try:
        r = requests.get("https://data-api.binance.vision/api/v3/ticker/24hr",
                         params={"symbols": json.dumps(SYMBOLS, separators=(",", ":"))},
                         timeout=8)
        val = {d["symbol"]: {"price": float(d["lastPrice"]),
                             "chg": float(d["priceChangePercent"])} for d in r.json()}
        _cache["prices"] = (time.time(), val)
    except Exception as e:
        print("[行情获取失败]", e)
    return val


def get_patterns():
    ts, val = _cache["patterns"]
    if val is not None and time.time() - ts < PATTERN_TTL:
        return val
    prices = get_prices() or {}
    streams = []
    for sym, tf, sl, tp in STREAMS:
        item = {"sym": sym, "tf": tf, "sl": sl, "tp": tp}
        try:
            df = ethbtc_mon.closed_bars(
                bt.fetch_klines(sym, tf, ethbtc_mon.LOOKBACK_DAYS[tf]),
                ethbtc_mon.TF_SECONDS[tf])
            df = bt.add_macd(df)
            df = bt.generate_signals(df)
            last = df.iloc[-1]
            l_txt, s_txt = ethbtc_mon.pattern_status(df)
            p = prices.get(sym, {})
            item.update({"close": float(last["close"]), "dif": float(last["dif"]),
                         "zone": "水上" if last["dif"] > 0 else "水下",
                         "long": l_txt, "short": s_txt,
                         "signal": int(last["signal"]),
                         "price": p.get("price"), "chg": p.get("chg")})
        except Exception as e:
            item["error"] = str(e)
        streams.append(item)
    gold_seq = None
    try:
        df = gold_mon.closed_bars(bt.fetch_klines("PAXGUSDT", "1h", gold_mon.LOOKBACK_DAYS))
        gold_seq = gold_mon.macd_last_seq(df["close"])
    except Exception:
        pass
    val = {"streams": streams, "gold_seq": gold_seq}
    _cache["patterns"] = (time.time(), val)
    return val


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def read_trades(path, account, limit=40):
    rows, spark = [], []
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return rows, spark
    for ln in lines:
        m = TS_RE.match(ln.strip())
        if not m:
            continue
        ts, msg = m.group(1), m.group(2)
        pnl = PNL_RE.search(msg)
        rows.append({"ts": ts, "account": account, "msg": msg,
                     "pnl": float(pnl.group(1)) if pnl else None})
        mb = BAL_RE.search(msg)
        if mb:
            spark.append(float(mb.group(1)))
    rows.reverse()
    return rows[:limit], spark


def gold_levels(st, price):
    res, sup, dead_n = [], [], 0
    for lv in st.get("levels", {}).values():
        if lv.get("dead"):
            dead_n += 1
            continue
        if lv.get("used"):
            continue
        dist = (lv["price"] / price - 1) * 100 if price else None
        (res if lv["kind"] == "res" else sup).append(
            {"price": lv["price"], "dist": dist})
    res.sort(key=lambda x: abs(x["dist"]) if x["dist"] is not None else 9e9)
    sup.sort(key=lambda x: abs(x["dist"]) if x["dist"] is not None else 9e9)
    return res, sup, dead_n


def build_status():
    prices = get_prices() or {}
    paxg = (prices.get("PAXGUSDT") or {}).get("price")

    pool = read_json("paper_state_pool.json")
    pos_out, total_unreal = [], 0.0
    for key, p in (pool.get("positions") or {}).items():
        price = (prices.get(key.split("|")[0]) or {}).get("price")
        unreal = None
        if price:
            chg = (price - p["entry"]) / p["entry"] * p["side"]
            unreal = NOTIONAL * chg - p.get("entry_fee", 4.0) - NOTIONAL * FEE_RATE
            total_unreal += unreal
        pos_out.append({"key": key, "sym": key.split("|")[0], "tf": key.split("|")[1],
                        "side": p["side"], "entry": p["entry"], "entry_time": p["entry_time"],
                        "sl": p["sl"], "tp": p["tp"], "price": price, "unreal": unreal,
                        "sl_pct": abs((p["sl"] / p["entry"] - 1)) * 100,
                        "tp_pct": abs((p["tp"] / p["entry"] - 1)) * 100})
    pool_trades, pool_spark = read_trades("paper_trades_pool.log", "BTC/ETH")
    pool_eq = pool.get("balance", 1000.0) + total_unreal

    gold = read_json("paper_state_gold_swing.json")
    gp = gold.get("position")
    gold_unreal, gold_pos = None, None
    if gp:
        gold_pos = {"side": gp["side"], "entry": gp["entry"], "entry_time": gp["entry_time"],
                    "sl": gp["sl"], "tp": gp["tp"], "price": paxg,
                    "sl_pct": abs((gp["sl"] / gp["entry"] - 1)) * 100,
                    "tp_pct": abs((gp["tp"] / gp["entry"] - 1)) * 100}
        if paxg:
            gold_unreal = NOTIONAL * ((paxg - gp["entry"]) / gp["entry"] * gp["side"])
            gold_pos["unreal"] = gold_unreal
    gold_trades, gold_spark = read_trades("paper_trades_gold_swing.log", "黄金")
    gold_eq = gold.get("balance", 1000.0) + (gold_unreal or 0.0)
    res, sup, dead_n = gold_levels(gold, paxg)

    alerts = []
    try:
        with open("signal_alerts.log", encoding="utf-8") as f:
            alerts = [l.strip() for l in f.readlines()[-15:]][::-1]
    except Exception:
        pass

    trades = sorted(pool_trades + gold_trades, key=lambda r: r["ts"], reverse=True)

    return {"ts": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "prices": prices,
            "pool": {"balance": pool.get("balance", 1000.0), "unreal": total_unreal,
                     "eq": pool_eq, "init": pool.get("init", 1000.0),
                     "positions": pos_out, "spark": pool_spark},
            "gold": {"balance": gold.get("balance", 1000.0), "unreal": gold_unreal,
                     "eq": gold_eq, "init": gold.get("init", 1000.0),
                     "position": gold_pos, "spark": gold_spark,
                     "res": res, "sup": sup, "dead_n": dead_n, "paxg": paxg},
            "patterns": get_patterns(),
            "trades": trades, "alerts": alerts}


HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>策略监控面板</title>
<style>
:root{--bg:#0d1117;--card:#161b22;--bd:#21262d;--fg:#e6edf3;--dim:#8b949e;
--up:#3fb950;--dn:#f85149;--blue:#58a6ff;--gold:#d4af37}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--fg);font:14px/1.5 "Segoe UI","Microsoft YaHei",sans-serif;padding:18px}
h1{font-size:20px;margin-bottom:2px}
.sub{color:var(--dim);font-size:12px;margin-bottom:16px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:14px 16px}
.card.gold{border-color:#5a4a1a}
.card h2{font-size:14px;color:var(--dim);font-weight:600;margin-bottom:8px}
.eq{font-size:26px;font-weight:700}
.eq small{font-size:13px;color:var(--dim);font-weight:400}
.up{color:var(--up)}.dn{color:var(--dn)}.blue{color:var(--blue)}
.muted{color:var(--dim)}
table{width:100%;border-collapse:collapse;font-size:13px;margin-top:6px}
th{color:var(--dim);text-align:left;font-weight:600;padding:4px 6px;border-bottom:1px solid var(--bd)}
td{padding:5px 6px;border-bottom:1px solid #1a2028}
tr:last-child td{border-bottom:none}
.badge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;font-weight:600}
.b-long{background:#12351c;color:var(--up)}
.b-short{background:#3d1618;color:var(--dn)}
.b-buy{background:#12351c;color:var(--up);animation:pulse 1.5s infinite}
.b-sell{background:#3d1618;color:var(--dn);animation:pulse 1.5s infinite}
.b-skip{background:#2a2f38;color:var(--dim)}
@keyframes pulse{50%{opacity:.55}}
.chip{display:inline-block;margin:3px 4px 3px 0;padding:3px 10px;border-radius:14px;font-size:12.5px;border:1px solid var(--bd)}
.chip.res{background:#251416;color:#ff9b9b;border-color:#4a2226}
.chip.sup{background:#12251a;color:#7ee2a8;border-color:#1f4a30}
svg{display:block;margin-top:6px}
.sec{margin-top:14px}
.al{font-family:Consolas,monospace;font-size:12px;color:#c9d1d9;padding:3px 0;border-bottom:1px solid #1a2028;word-break:break-all}
footer{text-align:center;color:var(--dim);font-size:11px;margin-top:18px}
.ok{color:var(--up)}.warn{color:#d29922}
@media(max-width:860px){.grid{grid-template-columns:1fr}}
</style>
</head>
<body>
<h1>策略监控面板 <span id="conn" class="muted" style="font-size:12px"></span></h1>
<div class="sub">模拟盘 · 行情来自币安公共镜像 · 20秒自动刷新(<span id="cd">20</span>s) · 更新于 <span id="upd">--</span></div>

<div class="grid">
  <div class="card" id="poolCard"></div>
  <div class="card gold" id="goldCard"></div>
</div>

<div class="grid sec">
  <div class="card"><h2>MACD 形态状态（ETH/BTC）</h2><div id="pat"></div></div>
  <div class="card gold"><h2>黄金关键位（PAXG <span id="paxgp">--</span>）</h2><div id="levels"></div></div>
</div>

<div class="grid sec">
  <div class="card"><h2>最近成交</h2><div id="trades"></div></div>
  <div class="card"><h2>信号提醒</h2><div id="alerts"></div></div>
</div>
<footer>strategy_dashboard · 数据每20秒刷新 · 端口 8765</footer>

<script>
const fmt=(x,d=2)=>x==null?'--':Number(x).toLocaleString('zh-CN',{minimumFractionDigits:d,maximumFractionDigits:d});
const cls=x=>x==null?'':(x>=0?'up':'dn');
const pnl=(x)=>x==null?'<span class="muted">--</span>':`<span class="${cls(x)}">${x>=0?'+':''}${fmt(x)}</span>`;
const sideTag=s=>s==1?'<span class="badge b-long">多</span>':'<span class="badge b-short">空</span>';
const seqTxt=s=>!s||!s.length?'不足3次':s.map(v=>v==1?'金叉':'死叉').join('→');

function spark(vals,init){
  if(!vals||vals.length<2) return '<div class="muted" style="font-size:12px">暂无成交轨迹</div>';
  const all=[init,...vals],w=560,h=42;
  const mn=Math.min(...all),mx=Math.max(...all),rg=(mx-mn)||1;
  const pts=all.map((v,i)=>`${(i/(all.length-1)*w).toFixed(1)},${(h-4-(v-mn)/rg*(h-8)).toFixed(1)}`).join(' ');
  const c=all[all.length-1]>=all[0]?'#3fb950':'#f85149';
  return `<svg width="100%" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><polyline points="${pts}" fill="none" stroke="${c}" stroke-width="2"/></svg>`;
}

function account(el,name,st,extra){
  const pc=(st.eq/st.init-1)*100;
  const rows=extra;
  el.innerHTML=`<h2>${name}</h2>
    <div class="eq">${fmt(st.eq)} <small>U</small>
      <span class="${cls(pc)}" style="font-size:15px;margin-left:8px">${pc>=0?'+':''}${pc.toFixed(1)}%</span></div>
    <div class="muted" style="font-size:12px;margin-top:2px">已实现余额 ${fmt(st.balance)}U · 浮盈 ${pnl(st.unreal)}U · 本金 ${fmt(st.init,0)}U</div>
    ${rows||''}
    ${spark(st.spark,st.init)}`;
}

function posTable(pos){
  if(!pos||!pos.length) return '<div class="muted" style="margin-top:8px;font-size:13px">当前无持仓</div>';
  return `<table><tr><th>标的</th><th>方向</th><th>入场</th><th>现价</th><th>浮盈</th><th>止损/止盈</th></tr>`+
    pos.map(p=>`<tr><td>${p.sym} <span class="muted">${p.tf}</span></td><td>${sideTag(p.side)}</td>
    <td>${fmt(p.entry)}</td><td>${fmt(p.price)}</td><td>${pnl(p.unreal)}</td>
    <td class="muted">${fmt(p.sl)}(${p.sl_pct.toFixed(2)}%) / ${fmt(p.tp)}(${p.tp_pct.toFixed(2)}%)</td></tr>`).join('')+
    `</table>`;
}

function render(s){
  document.getElementById('upd').textContent=s.ts;
  document.getElementById('conn').innerHTML='<span class="ok">● 在线</span>';
  const P=s.prices||{};
  account(document.getElementById('poolCard'),'BTC/ETH 共享资金池',s.pool,posTable(s.pool.positions));
  const gp=s.gold.position;
  const grow=gp?`<table><tr><th>方向</th><th>入场</th><th>现价</th><th>浮盈</th><th>止损/止盈</th></tr>
    <tr><td>${sideTag(gp.side)}</td><td>${fmt(gp.entry)}</td><td>${fmt(gp.price)}</td><td>${pnl(gp.unreal)}</td>
    <td class="muted">${fmt(gp.sl)}(${gp.sl_pct.toFixed(2)}%) / ${fmt(gp.tp)}(${gp.tp_pct.toFixed(2)}%)</td></tr></table>`
    :'<div class="muted" style="margin-top:8px;font-size:13px">当前无持仓</div>';
  account(document.getElementById('goldCard'),'黄金 关键位反转+MACD序列确认',s.gold,grow);
  document.getElementById('paxgp').textContent=fmt(s.gold.paxg);

  // 形态
  const pt=s.patterns||{};
  let ph='<table><tr><th>标的</th><th>价格</th><th>24h</th><th>DIF(区)</th><th>多头形态</th><th>空头形态</th></tr>';
  (pt.streams||[]).forEach(t=>{
    const sig=t.signal==1?' <span class="badge b-buy">买点</span>':t.signal==-1?' <span class="badge b-sell">卖点</span>':'';
    const px=t.price!=null?fmt(t.price):'--';
    const ch=t.chg!=null?`<span class="${cls(t.chg)}">${t.chg>=0?'+':''}${t.chg.toFixed(2)}%</span>`:'--';
    ph+=`<tr><td>${t.sym} <span class="muted">${t.tf}</span>${sig}</td><td>${px}</td><td>${ch}</td>
      <td>${t.dif!=null?t.dif.toFixed(2):'--'} <span class="muted">(${t.zone||'--'})</span></td>
      <td>${t.long||'--'}</td><td>${t.short||'--'}</td></tr>`;
  });
  ph+='</table>';
  document.getElementById('pat').innerHTML=ph;

  // 关键位
  const g=s.gold;
  const chip=(lv,kind)=>`<span class="chip ${kind}">${kind=='res'?'阻':'支'} ${fmt(lv.price)} (${lv.dist>=0?'+':''}${lv.dist.toFixed(2)}%)</span>`;
  document.getElementById('levels').innerHTML=
    `<div style="margin:4px 0 2px" class="muted">MACD 最近3叉: <b>${seqTxt(pt.gold_seq)}</b></div>`
    +(g.res.length?'<div>'+g.res.slice(0,4).map(l=>chip(l,'res')).join('')+'</div>':'<div class="muted" style="font-size:12px">无活跃阻力</div>')
    +(g.sup.length?'<div>'+g.sup.slice(0,4).map(l=>chip(l,'sup')).join('')+'</div>':'<div class="muted" style="font-size:12px">无活跃支撑</div>')
    +`<div class="muted" style="font-size:11px;margin-top:4px">已失效关键位 ${g.dead_n} 个 · 按距离现价排序</div>`;

  // 成交
  document.getElementById('trades').innerHTML=(s.trades&&s.trades.length)?
    '<table><tr><th>时间</th><th>账户</th><th>操作</th></tr>'+s.trades.slice(0,14).map(t=>{
      const act=t.msg.includes('平仓')?'<span class="badge b-skip">平仓</span>'
        :t.msg.includes('跳过')?'<span class="badge b-skip">跳过</span>':'<span class="badge b-buy">开仓</span>';
      const body=t.msg.replace(/^\[[^\]]+\]\s*/,'').replace('模拟','');
      return `<tr><td class="muted" style="white-space:nowrap">${t.ts.slice(5,16)}</td>
        <td>${t.account=='黄金'?'<span style="color:var(--gold)">黄金</span>':'<span class="blue">池</span>'}</td>
        <td>${act} <span style="font-size:12.5px">${body}</span></td></tr>`;
    }).join('')+'</table>'
    :'<div class="muted" style="font-size:13px">暂无成交记录</div>';

  document.getElementById('alerts').innerHTML=(s.alerts&&s.alerts.length)?
    s.alerts.map(a=>`<div class="al">${a.replace(/!+/g,'')}</div>`).join('')
    :'<div class="muted" style="font-size:13px">暂无信号</div>';
}

let cd=20;
async function refresh(){
  try{
    const r=await fetch('/api/status');
    const s=await r.json();
    render(s);cd=20;
  }catch(e){
    document.getElementById('conn').innerHTML='<span class="warn">● 连接失败, 重试中</span>';cd=5;
  }
}
setInterval(()=>{cd--;document.getElementById('cd').textContent=cd;if(cd<=0)refresh();},1000);
refresh();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype, code=200):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/api/status":
            try:
                self._send(json.dumps(build_status(), ensure_ascii=False),
                           "application/json; charset=utf-8")
            except Exception as e:
                self._send(json.dumps({"error": str(e)}), "application/json", 500)
        elif self.path == "/":
            self._send(HTML, "text/html; charset=utf-8")
        else:
            self._send("not found", "text/plain", 404)


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("面板已启动: http://localhost:{}  (Ctrl+C 停止)".format(PORT))
    srv.serve_forever()
