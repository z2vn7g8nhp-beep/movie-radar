"""推送：Bark（iPhone）。BARK_KEY 可以填多个，用英文逗号隔开。"""
import os
import time

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


def push(title, body, url=None, level=None, call=False, tries=3):
    """tries：一个设备最多试几次。Bark 偶尔会超时，当场多试两次，不用等下一轮（开票提醒抢的就是这几分钟）。"""
    keys = bark_keys()
    if not keys:
        print(f"[推送-未配置BARK_KEY] {title} | {body}")
        return False
    # 没设 BARK_SERVER 变量时 GitHub 会传进空字符串，不能直接拿来用（以前就是这里导致推送全部失败）
    server = (os.environ.get("BARK_SERVER") or "").strip().rstrip("/") or "https://api.day.app"
    if not server.startswith(("http://", "https://")):
        server = "https://" + server
    ok = True
    for k in keys:
        payload = {"device_key": k, "title": title, "body": body, "group": "电影雷达"}  # 不指定 sound，用 Bark 默认的普通提示音
        if url:
            payload["url"] = url
        if level:
            payload["level"] = level
            if level == "critical":
                payload["volume"] = 6
        if call:
            payload["call"] = "1"  # Bark 持续响铃约 30 秒
        sent = False
        for attempt in range(max(1, tries)):
            if attempt:
                time.sleep(2 + attempt * 2)
            try:
                r = requests.post(server + "/push", json=payload, timeout=12)
                if r.status_code == 200:
                    sent = True
                    break
                print(f"[推送失败] HTTP {r.status_code} {r.text[:120]}")
                if 400 <= r.status_code < 500 and r.status_code != 429:
                    break  # 密钥或参数不对，重试也没用
            except requests.RequestException as e:
                print(f"[推送失败] {e.__class__.__name__}")
        ok = ok and sent
    print(f"[{'推送' if ok else '推送没发出去'}] {title} | {body}")
    return ok
