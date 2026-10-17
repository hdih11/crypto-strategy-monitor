# -*- coding: utf-8 -*-
"""GitHub OAuth 设备流：申请设备码并轮询拿 token（绕开 gh 交互式 tty 要求）。
token 写入系统临时目录，不写入项目目录。"""
import json
import os
import time

import requests

CLIENT_ID = "178c6fc778ccc68e1d6a"  # GitHub CLI 官方公开 OAuth App ID
OUT = os.path.join(os.environ["TEMP"], "gh_device_token.txt")

S = requests.Session()
S.trust_env = False  # 绕过失效的代理环境变量，直连 GitHub

r = S.post("https://github.com/login/device/code",
           data={"client_id": CLIENT_ID, "scope": "repo read:org"},
           headers={"Accept": "application/json"}, timeout=20)
d = r.json()
print("VERIFY_URL:", d["verification_uri"])
print("USER_CODE:", d["user_code"])
print("EXPIRES_IN:", d.get("expires_in"), "s")
print("INTERVAL:", d.get("interval", 5))

deadline = time.time() + d.get("expires_in", 899)
interval = d.get("interval", 5)
while time.time() < deadline:
    time.sleep(interval)
    p = S.post("https://github.com/login/oauth/access_token",
               data={"client_id": CLIENT_ID,
                     "device_code": d["device_code"],
                     "grant_type": "urn:ietf:params:oauth:grant-type:device_code"},
               headers={"Accept": "application/json"}, timeout=20).json()
    err = p.get("error")
    if err == "authorization_pending":
        continue
    if err == "slow_down":
        interval += 5
        continue
    if p.get("access_token"):
        with open(OUT, "w") as f:
            f.write(p["access_token"])
        print("TOKEN_OK")
        break
    print("AUTH_ERROR:", err, p.get("error_description"))
    break
else:
    print("TIMEOUT_WAITING_AUTH")
