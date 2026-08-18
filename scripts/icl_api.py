#!/usr/bin/env python3
"""ICL API - 測試官方 endpoint"""
import subprocess, json
from pathlib import Path

key = ""
for line in (Path(__file__).parent.parent / ".env").read_text().splitlines():
    if line.startswith("ICL_ACCESS_KEY="):
        key = line.split("=", 1)[1].strip()

BASE = "https://intensivecolearn.ing/api/v1"
PROG = "b43d2e97-ed88-4ca3-b12f-7ef672b01205"
auth = "Authorization: Bearer " + key

def get(path):
    r = subprocess.run(
        ["curl", "-s", BASE + path, "-H", auth],
        capture_output=True, text=True
    )
    try:
        return json.loads(r.stdout)
    except Exception:
        return {"_raw": r.stdout[:200]}

# 1. 我的打卡
print("=== GET /me/check-ins ===")
res = get("/me/check-ins?programId=" + PROG)
if "data" in res:
    data = res["data"]
    items = data.get("items", data) if isinstance(data, dict) else data
    if isinstance(items, list):
        print("打卡數量:", len(items))
        for c in items[:5]:
            ts = str(c.get("createdAt", "?"))[:10]
            content = str(c.get("content", ""))[:80]
            print("  -", ts, "|", content)
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2)[:400])
else:
    print(json.dumps(res, ensure_ascii=False, indent=2)[:300])

# 2. 課程活動
print("\n=== GET /programs/{id}/events ===")
res = get("/programs/" + PROG + "/events")
if "data" in res:
    data = res["data"]
    items = data.get("items", data) if isinstance(data, dict) else data
    if isinstance(items, list):
        print("活動數量:", len(items))
        for e in items[:5]:
            ts = str(e.get("startsAt", "?"))[:16]
            title = e.get("title", "?")
            print("  -", ts, "|", title)
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2)[:400])
else:
    print(json.dumps(res, ensure_ascii=False, indent=2)[:300])
