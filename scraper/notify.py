"""推送：Bark（iPhone）。BARK_KEY 可以填多个，用英文逗号隔开。"""
import os

import requests


def bark_keys():
    raw = os.environ.get("BARK_KEY", "")
    keys = []
    for k in raw.replace("\n", ",").split(","):
        k = k.strip()
        if not k:
            continue
        # 允许直接粘贴 Bark 里复制出来的整条网址，如 https://api.day.app/abcd1234/
        if k.startswith("http"):
            k = k.rstrip("/").split("/")[-1] if k.count("/") >= 3 else k
        keys.append(k)
    return keys


def push(title, body, url=None, level=None, call=False):
    keys = bark_keys()
    if not keys:
        print(f"[推送-未配置BARK_KEY] {title} | {body}")
        return False
    server = os.environ.get("BARK_SERVER", "https://api.day.app").rstrip("/")
    ok = True
    for k in keys:
        payload = {"device_key": k, "title": title, "body": body, "group": "电影雷达",
                   "sound": "minuet"}
        if url:
            payload["url"] = url
        if level:
            payload["level"] = level
            if level == "critical":
                payload["volume"] = 6
        if call:
            payload["call"] = "1"  # Bark 持续响铃约 30 秒
        try:
            r = requests.post(server + "/push", json=payload, timeout=15)
            if r.status_code != 200:
                ok = False
                print(f"[推送失败] HTTP {r.status_code} {r.text[:120]}")
        except requests.RequestException as e:
            ok = False
            print(f"[推送失败] {e}")
    print(f"[推送] {title} | {body}")
    return ok
