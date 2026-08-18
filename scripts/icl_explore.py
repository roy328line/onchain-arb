#!/usr/bin/env python3
"""探索 ICL 平台打卡相關 endpoint"""
import subprocess, json
from pathlib import Path

key = ""
for line in Path(__file__).parent.parent.joinpath(".env").read_text().splitlines():
    if line.startswith("ICL_ACCESS_KEY="):
        key = line.split("=", 1)[1].strip()

BASE = "https://intensivecolearn.ing/api/v1"
auth = f"Authorization: Bearer {key}"
ct   = "Content-Type: application/json"
prog_id = "b43d2e97-ed88-4ca3-b12f-7ef672b01205"

def req(method, path, body=None):
    args = ["curl", "-s", "-X", method, "-H", auth, "-H", ct]
    if body:
        args += ["-d", body]
    args.append(BASE + path)
    r = subprocess.run(args, capture_output=True, text=True)
    try:
        res = json.loads(r.stdout)
        if "not_found" in str(res.get("error", "")):
            return "404"
        elif "error" in res:
            return f"ERR: {res['error']}"
        else:
            return f"OK: {list(res.keys())}"
    except Exception:
        return f"raw({len(r.stdout)}): {r.stdout[:60]}"

tests = [
    ("POST", f"/programs/{prog_id}/checkin",   '{"content":"test"}'),
    ("POST", f"/programs/{prog_id}/check-in",  '{"content":"test"}'),
    ("POST", f"/programs/{prog_id}/log",       '{"content":"test"}'),
    ("POST", "/checkin",   '{"programId":"' + prog_id + '","content":"test"}'),
    ("POST", "/log",       '{"programId":"' + prog_id + '","content":"test"}'),
    ("GET",  f"/programs/{prog_id}/checkins",  None),
    ("GET",  f"/programs/{prog_id}/logs",      None),
    ("GET",  f"/programs/{prog_id}/members",   None),
    ("GET",  f"/programs/{prog_id}/my-log",    None),
    ("GET",  f"/programs/{prog_id}/my-checkin",None),
]

for method, path, body in tests:
    result = req(method, path, body)
    print(f"[{method:4}] {path}")
    print(f"        => {result}")
