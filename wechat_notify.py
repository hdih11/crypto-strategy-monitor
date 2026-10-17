# -*- coding: utf-8 -*-
"""
微信消息推送模块（供 macd_signal_monitor.py 调用）

支持三种通道，在 wechat_config.json 中配置任意一个或多个即可（不配则跳过，不影响监控）:

1) PushPlus（推荐，最简单）: 微信扫码登录 https://www.pushplus.plus -> 一对一推送 ->
   复制 token，配置 {"pushplus_token": "你的token"}；消息经微信公众号"pushplus推送加"送达。

2) Server酱 Turbo: 登录 https://sct.ftqq.com -> SendKey，配置
   {"serverchan_key": "SCT开头的key"}；消息经公众号"方糖"送达。

3) 企业微信群机器人: 在企业微信群 -> 群设置 -> 群机器人 -> 添加 -> 复制 Webhook key，
   配置 {"wecom_bot_key": "key字符串"}；消息发到该群（需企业微信/个人微信关联企业微信）。

wechat_config.json 示例:
{
  "pushplus_token": "xxxxxxxxxxxxxxxx",
  "serverchan_key": "",
  "wecom_bot_key": ""
}
"""
import json
import os

import requests

CONFIG_PATH = "wechat_config.json"
TIMEOUT = 8
_config_cache = None


def _load_config():
    global _config_cache
    if _config_cache is not None:
        return _config_cache
    if not os.path.exists(CONFIG_PATH):
        _config_cache = {}
        return _config_cache
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        _config_cache = json.load(f)
    return _config_cache


def _pushplus(token, title, content, topic=""):
    body = {"token": token, "title": title, "content": content, "template": "html"}
    if topic:  # 群组编码：发送到 PushPlus 群组（群成员关注该主题后都能收到）
        body["topic"] = topic
    r = requests.post("https://www.pushplus.plus/send", json=body, timeout=TIMEOUT)
    data = r.json()
    if data.get("code") != 200:
        raise RuntimeError("pushplus: {}".format(data.get("msg") or data))


def _serverchan(key, title, content):
    r = requests.post("https://sctapi.ftqq.com/{}.send".format(key),
                      data={"title": title, "desp": content}, timeout=TIMEOUT)
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError("serverchan: {}".format(data.get("message") or data))


def _wecom_bot(bot_key, title, content):
    r = requests.post(
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send",
        params={"key": bot_key},
        json={"msgtype": "markdown",
              "markdown": {"content": "### {}\n{}".format(title, content.replace("\n", "\n\n"))}},
        timeout=TIMEOUT)
    data = r.json()
    if data.get("errcode") != 0:
        raise RuntimeError("wecom_bot: {}".format(data.get("errmsg") or data))


def send(title, content):
    """向所有已配置通道发送；返回 (成功数, 失败信息列表)。任何异常不外抛。"""
    cfg = _load_config()
    ok, errs = 0, []
    channels = [
        ("pushplus_token", _pushplus),
        ("serverchan_key", _serverchan),
        ("wecom_bot_key", _wecom_bot),
    ]
    for field, fn in channels:
        key = (cfg.get(field) or "").strip()
        if not key:
            continue
        try:
            topic = (cfg.get("pushplus_topic") or "").strip() if field == "pushplus_token" else ""
            fn(key, title, content, topic) if field == "pushplus_token" else fn(key, title, content)
            ok += 1
        except Exception as e:
            errs.append("{}: {}".format(field, e))
    return ok, errs


if __name__ == "__main__":
    n, errs = send("MACD监控测试", "如果你收到这条消息，说明微信推送配置成功。")
    if n == 0:
        print("未发送：请检查 {} 是否已配置（当前错误: {}）".format(
            CONFIG_PATH, "; ".join(errs) if errs else "无任何已配置通道"))
    else:
        print("已发送到 {} 个通道{}".format(n, ("，错误: " + "; ".join(errs)) if errs else ""))
