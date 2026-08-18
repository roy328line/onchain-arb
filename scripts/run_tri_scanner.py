#!/usr/bin/env python3
"""啟動 tri_scanner，自動從 .env 讀取 KEY"""
import os, sys, subprocess
from pathlib import Path

env_file = Path('/home/ubuntu/onchain-arb/.env')
for line in env_file.read_text().splitlines():
    if '=' in line and not line.startswith('#'):
        k, v = line.split('=', 1)
        os.environ.setdefault(k.strip(), v.strip())

os.environ['SCAN_SECONDS'] = os.environ.get('SCAN_SECONDS', '1800')
os.environ['MIN_POOL_TVL'] = os.environ.get('MIN_POOL_TVL', '50000')

sys.path.insert(0, '/home/ubuntu/onchain-arb')
from scripts.tri_scanner import scan
scan()
