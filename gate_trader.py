# -*- coding: utf-8 -*-
"""
Gate.io USDT 永续合约实盘交易模块（供 macd_signal_monitor.py 调用）

- 认证: API v4 HMAC-SHA512 签名，密钥从 gate_config.json 或环境变量读取
- 模式: 全仓(cross, leverage=0)，单向持仓
- 开仓: 市价单 + 止损/止盈两张条件触发单(reduce_only)
- 合约: BTC_USDT/ETH_USDT，quanto=0.0001（1张=0.0001币）

gate_config.json 格式（与脚本同目录，勿提交/勿外泄）:
    {"api_key": "xxx", "api_secret": "yyy"}
"""
import hashlib
import hmac
import json
import os
import time

import requests

BASE = "https://api.gateio.ws"
SETTLE = "usdt"


class GateFutures:
    def __init__(self, api_key=None, api_secret=None, config_path="gate_config.json"):
        if not api_key and os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            api_key, api_secret = cfg.get("api_key"), cfg.get("api_secret")
        api_key = api_key or os.environ.get("GATE_API_KEY")
        api_secret = api_secret or os.environ.get("GATE_API_SECRET")
        if not api_key or not api_secret:
            raise RuntimeError("缺少 Gate API 密钥：请创建 gate_config.json 或设置环境变量 GATE_API_KEY/GATE_API_SECRET")
        self.key, self.secret = api_key, api_secret

    # ---------- 签名与请求 ----------
    def _sign(self, method, path, query, body):
        payload_hash = hashlib.sha512((body or "").encode()).hexdigest()
        ts = str(int(time.time()))
        s = "{}\n{}\n{}\n{}\n{}".format(method, path, query or "", payload_hash, ts)
        sign = hmac.new(self.secret.encode(), s.encode(), hashlib.sha512).hexdigest()
        return {"KEY": self.key, "Timestamp": ts, "SIGN": sign,
                "Content-Type": "application/json"}

    def _req(self, method, path, query="", body=None):
        body_str = json.dumps(body) if body is not None else ""
        headers = self._sign(method, path, query, body_str)
        r = requests.request(method, BASE + path + (("?" + query) if query else ""),
                             data=body_str or None, headers=headers, timeout=15)
        if r.status_code >= 400:
            raise RuntimeError("Gate API {} {} -> {}: {}".format(method, path, r.status_code, r.text[:300]))
        return r.json() if r.text else {}

    # ---------- 公共行情 ----------
    @staticmethod
    def contract_info(contract):
        r = requests.get("{}/api/v4/futures/{}/contracts/{}".format(BASE, SETTLE, contract), timeout=15)
        r.raise_for_status()
        return r.json()

    # ---------- 账户与持仓 ----------
    def get_position(self, contract):
        return self._req("GET", "/api/v4/futures/{}/positions/{}".format(SETTLE, contract))

    def set_cross_margin(self, contract):
        """leverage=0 即全仓模式。"""
        return self._req("POST", "/api/v4/futures/{}/positions/{}/leverage".format(SETTLE, contract),
                         query="leverage=0")

    # ---------- 下单 ----------
    def market_order(self, contract, size, reduce_only=False):
        """size>0 开/加多, <0 开/加空；reduce_only=True 只平仓。"""
        body = {"contract": contract, "size": size, "price": "0", "tif": "ioc"}
        if reduce_only:
            body["reduce_only"] = True
        return self._req("POST", "/api/v4/futures/{}/orders".format(SETTLE), body=body)

    def place_trigger(self, contract, size, trigger_price, rule):
        """条件单: rule=1 价格>=触发, rule=2 价格<=触发；触发后市价 reduce_only 平仓。"""
        body = {
            "initial": {"contract": contract, "size": size, "price": "0",
                        "tif": "ioc", "reduce_only": True},
            "trigger": {"strategy_type": 0, "price_type": 0,
                        "price": "{:.2f}".format(trigger_price), "rule": rule},
        }
        return self._req("POST", "/api/v4/futures/{}/price_orders".format(SETTLE), body=body)

    def cancel_all_triggers(self, contract):
        return self._req("DELETE", "/api/v4/futures/{}/price_orders".format(SETTLE),
                         query="contract=" + contract)

    # ---------- 组合操作 ----------
    def open_position(self, contract, side, notional_usdt, price, sl_price, tp_price):
        """side=1 做多 / -1 做空。返回 (下单结果, 止损单, 止盈单, 张数)。
        张数 = notional / (price * quanto)，不足1张抛异常。"""
        info = self.contract_info(contract)
        quanto = float(info["quanto_multiplier"])
        size = int(notional_usdt / (price * quanto))
        if size < int(info["order_size_min"]):
            raise RuntimeError("名义价值{}U太小，折合{}张 < 最小下单{}张".format(
                notional_usdt, size, info["order_size_min"]))
        self.set_cross_margin(contract)

        # 已有反向持仓先平掉
        pos = self.get_position(contract)
        cur = int(pos.get("size", 0) or 0)
        if cur != 0 and (cur > 0) != (side > 0):
            self.cancel_all_triggers(contract)
            self.market_order(contract, -cur, reduce_only=True)
            time.sleep(1)

        order = self.market_order(contract, size * side)
        close_size = -size * side
        # 多头: 止损=价格<=sl(rule2), 止盈=价格>=tp(rule1)；空头相反
        sl_rule, tp_rule = (2, 1) if side == 1 else (1, 2)
        sl_ord = self.place_trigger(contract, close_size, sl_price, sl_rule)
        tp_ord = self.place_trigger(contract, close_size, tp_price, tp_rule)
        return order, sl_ord, tp_ord, size

    def close_all(self, contract):
        pos = self.get_position(contract)
        cur = int(pos.get("size", 0) or 0)
        if cur == 0:
            return None
        self.cancel_all_triggers(contract)
        return self.market_order(contract, -cur, reduce_only=True)


if __name__ == "__main__":
    # 无密钥自检：合约规格与仓位张数换算
    for c in ("BTC_USDT", "ETH_USDT"):
        info = GateFutures.contract_info(c)
        mark = float(info.get("mark_price", 0))
        q = float(info["quanto_multiplier"])
        size = int(10000 / (mark * q))
        print("{} mark={} quanto={} | 100U x100 = 10000U名义 -> {} 张 (最小{})".format(
            c, mark, q, size, info["order_size_min"]))
