#!/usr/bin/env python3
import subprocess, json, os
from pathlib import Path

env_path = Path(__file__).parent.parent / ".env"
key = ""
for line in env_path.read_text().splitlines():
    if line.startswith("ICL_ACCESS_KEY="):
        key = line.split("=", 1)[1].strip()

BASE = "https://intensivecolearn.ing/api/v1"
headers = ["-H", f"Authorization: Bearer {key}"]

def get(path):
    r = subprocess.run(["curl", "-s"] + headers + [BASE + path], capture_output=True, text=True)
    return json.loads(r.stdout)

# 1. 身份確認
me = get("/me")["data"]
print(f"✅ 連通成功：{me['name']} / {me['email']}")

# 2. 課程資訊
prog_id = "b43d2e97-ed88-4ca3-b12f-7ef672b01205"
p = get(f"/programs/{prog_id}")["data"]
print(f"\n📚 課程：{p['name']}")
print(f"   狀態：{p['lifecycleStatus']}")
print(f"   期間：{p['startDate']} ~ {p['endDate']}")
print(f"   每週缺席上限：{p['leaveAllowancePerWeek']} 天")
print(f"   申請狀態：{p['viewerApplication']['status']}")

# 3. 探索打卡 endpoint
print("\n🔍 探索打卡相關 endpoint...")
test_paths = [
    f"/programs/{prog_id}/checkin",
    f"/programs/{prog_id}/check-in",
    f"/programs/{prog_id}/log",
    f"/checkin",
    f"/log",
    f"/programs/{prog_id}/submit",
]
for path in test_paths:
    r = subprocess.run(
        ["curl", "-s", "-X", "OPTIONS"] + headers + [BASE + path],
        capture_output=True, text=True
    )
    try:
        res = json.loads(r.stdout)
        status = "404" if "not_found" in str(res) else "OK"
    except:
        status = "parse error"
    print(f"   OPTIONS {path} => {status}")
