#!/usr/bin/env python3
"""探索 ICL 平台能否讀取同學打卡記錄"""
import subprocess, json
from pathlib import Path

env_path = Path(__file__).parent.parent / ".env"
key = ""
for line in env_path.read_text().splitlines():
    if line.startswith("ICL_ACCESS_KEY="):
        key = line.split("=", 1)[1].strip()

BASE = "https://intensivecolearn.ing/api/v1"
auth_header = "Authorization: Bearer " + key
ct_header = "Content-Type: application/json"
prog_id = "b43d2e97-ed88-4ca3-b12f-7ef672b01205"

def req(method, path, body=None):
    args = ["curl", "-s", "-X", method, "-H", auth_header, "-H", ct_header]
    if body:
        args += ["-d", body]
    args.append(BASE + path)
    r = subprocess.run(args, capture_output=True, text=True)
    try:
        res = json.loads(r.stdout)
        if "not_found" in str(res.get("error", "")):
            return None, "404"
        elif "error" in res:
            return None, "ERR: " + str(res["error"])
        else:
            return res, "OK"
    except Exception:
        return None, "raw: " + r.stdout[:80]

print("=== 探索同學打卡/活動記錄 endpoint ===\n")

tests = [
    ("GET", "/programs/" + prog_id + "/activities"),
    ("GET", "/programs/" + prog_id + "/posts"),
    ("GET", "/programs/" + prog_id + "/submissions"),
    ("GET", "/programs/" + prog_id + "/checkins"),
    ("GET", "/programs/" + prog_id + "/entries"),
    ("GET", "/programs/" + prog_id + "/notes"),
    ("GET", "/programs/" + prog_id + "/updates"),
    ("GET", "/programs/" + prog_id + "/feed"),
    ("GET", "/programs/" + prog_id + "/timeline"),
    ("GET", "/programs/" + prog_id + "/participant-logs"),
    ("GET", "/programs/" + prog_id + "/members"),
    ("GET", "/activities"),
    ("GET", "/feed"),
    ("GET", "/posts"),
    ("GET", "/submissions"),
]

found_any = False
for method, path in tests:
    data, status = req(method, path)
    if status == "OK":
        found_any = True
        print("FOUND: [" + method + "] " + path)
        if "data" in data:
            d = data["data"]
            if isinstance(d, list):
                print("  items: " + str(len(d)))
                if d:
                    print("  first item keys: " + str(list(d[0].keys())[:8]))
            elif isinstance(d, dict):
                print("  data keys: " + str(list(d.keys())[:8]))
    else:
        print("  [" + method + "] " + path + " => " + status)

if not found_any:
    print("\n結論：API 無法讀取同學打卡記錄")
