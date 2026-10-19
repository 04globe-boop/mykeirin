"""ページの構造確認用：指定したURLを取得してそのまま保存する（解析は手元で行う）。"""
import os
import sys
import time

import requests

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
os.makedirs("probe", exist_ok=True)
for i, line in enumerate(sys.argv[1:]):
    name, url = line.split("=", 1)
    try:
        r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "ja"}, timeout=30)
        open(f"probe/{name}.html", "wb").write(r.content)
        print(name, r.status_code, len(r.content), r.headers.get("content-type"))
    except Exception as e:  # noqa: BLE001
        print(name, "ERROR", e)
    time.sleep(1.0)
