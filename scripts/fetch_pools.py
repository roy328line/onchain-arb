"""
fetch_pools.py — 從公開 RPC 抓取 Uniswap v2 / Sushiswap 池的真實 reserves
並存入 arb.duckdb 的 pool_snapshots 表。

執行方式：
    cd /home/ubuntu/onchain-arb
    python3 scripts/fetch_pools.py

設計原則：
- 只用標準庫 + duckdb（不需要 web3.py，避免安裝依賴）
- getReserves() = ABI selector 0x0902f1ac，直接 eth_call
- token0 / token1 順序：Uniswap v2 依地址排序，需注意方向
- 多節點 fallback：publicnode → 1rpc → mevblocker
"""

from __future__ import annotations

import json
import sys
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))
from scripts.db import get_conn

# ──────────────────────────────────────────────
# 1. 設定：監控的池清單
# ──────────────────────────────────────────────

@dataclass
class PoolConfig:
    name: str           # 人類可讀名稱，e.g. "uni_v2_usdc_weth"
    dex: str            # e.g. "uniswap_v2", "sushiswap"
    chain: str          # e.g. "ethereum"
    pool_addr: str      # 0x...（checksum or lowercase 都行）
    token0_sym: str     # 池裡 token0 的 symbol（依地址排序，需手動確認）
    token1_sym: str
    token0_decimals: int
    token1_decimals: int
    fee_bps: int        # 手續費 bps，e.g. 30 = 0.3%

# 監控清單：USDC/WETH 跨 DEX 套利對
POOLS: list[PoolConfig] = [
    PoolConfig(
        name="uni_v2_usdc_weth",
        dex="uniswap_v2",
        chain="ethereum",
        pool_addr="0xb4e16d0168e52d35cacd2c6185b44281ec28c9dc",
        token0_sym="USDC",      # token0 = USDC (低地址)
        token1_sym="WETH",      # token1 = WETH
        token0_decimals=6,
        token1_decimals=18,
        fee_bps=30,
    ),
    PoolConfig(
        name="sushi_usdc_weth",
        dex="sushiswap",
        chain="ethereum",
        pool_addr="0x397ff1542f962076d0bfe58ea045ffa2d347aca0",
        token0_sym="USDC",      # token0 = USDC
        token1_sym="WETH",      # token1 = WETH
        token0_decimals=6,
        token1_decimals=18,
        fee_bps=30,
    ),
    PoolConfig(
        name="uni_v2_usdt_weth",
        dex="uniswap_v2",
        chain="ethereum",
        pool_addr="0x0d4a11d5eeaac28ec3f61d100daf4d40471f1852",
        token0_sym="WETH",      # token0 = WETH (低地址)
        token1_sym="USDT",      # token1 = USDT
        token0_decimals=18,
        token1_decimals=6,
        fee_bps=30,
    ),
    PoolConfig(
        name="uni_v2_dai_weth",
        dex="uniswap_v2",
        chain="ethereum",
        pool_addr="0xa478c2975ab1ea89e8196811f51a7b7ade33eb11",
        token0_sym="DAI",       # token0 = DAI
        token1_sym="WETH",      # token1 = WETH
        token0_decimals=18,
        token1_decimals=18,
        fee_bps=30,
    ),
]

# ──────────────────────────────────────────────
# 2. RPC 層：eth_call getReserves()
# ──────────────────────────────────────────────

RPC_NODES = [
    "https://ethereum.publicnode.com",
    "https://1rpc.io/eth",
    "https://rpc.mevblocker.io",
    "https://eth-mainnet.public.blastapi.io",
]

HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "onchain-arb-scanner/0.1",
}

GET_RESERVES_SELECTOR = "0x0902f1ac"


def eth_call(to: str, data: str, block_hex: str = "latest", timeout: int = 6) -> Optional[str]:
    """呼叫 eth_call，帶多節點 fallback。回傳 hex result 或 None。
    block_hex：指定區塊（e.g. "0x188C2A6"），傳入同一個值確保批次一致性。
    """
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "eth_call",
        "params": [{"to": to, "data": data}, block_hex]
    }).encode()

    for node in RPC_NODES:
        try:
            req = urllib.request.Request(node, payload, HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result = json.loads(resp.read())
            if "result" in result and result["result"] and len(result["result"]) > 10:
                return result["result"]
        except Exception as e:
            print(f"  ⚠️  {node} failed: {e}", file=sys.stderr)

    return None


def get_block_number() -> Optional[int]:
    """取得最新區塊號碼。"""
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []
    }).encode()
    for node in RPC_NODES:
        try:
            req = urllib.request.Request(node, payload, HEADERS)
            with urllib.request.urlopen(req, timeout=6) as resp:
                result = json.loads(resp.read())
            if "result" in result:
                return int(result["result"], 16)
        except Exception:
            pass
    return None


def fetch_reserves(pool: PoolConfig, block_hex: str = "latest") -> Optional[dict]:
    """
    抓取單一池的 reserves，回傳標準化後的字典。
    block_hex：與批次其他池傳同一個值，確保同 block 一致性。

    getReserves() 回傳 ABI 編碼：
      [0:64]   reserve0 (uint112)
      [64:128] reserve1 (uint112)
      [128:192] blockTimestampLast (uint32)
    """
    raw = eth_call(pool.pool_addr, GET_RESERVES_SELECTOR, block_hex=block_hex)
    if raw is None:
        return None

    hex_str = raw[2:]  # strip 0x
    if len(hex_str) < 128:
        return None

    reserve0_raw = int(hex_str[0:64], 16)
    reserve1_raw = int(hex_str[64:128], 16)

    # 換算為人類可讀單位
    reserve0 = reserve0_raw / (10 ** pool.token0_decimals)
    reserve1 = reserve1_raw / (10 ** pool.token1_decimals)

    # Spot price: token0 以 token1 計價
    spot_price = reserve1 / reserve0 if reserve0 > 0 else 0.0

    # block 號從 hex 還原（"latest" 時為 None，由 main() 填入）
    block_num = int(block_hex, 16) if block_hex != "latest" else None

    return {
        "pool_addr":    pool.pool_addr,
        "name":         pool.name,
        "dex":          pool.dex,
        "chain":        pool.chain,
        "token0_sym":   pool.token0_sym,
        "token1_sym":   pool.token1_sym,
        "reserve0":     reserve0,
        "reserve1":     reserve1,
        "reserve0_raw": reserve0_raw,
        "reserve1_raw": reserve1_raw,
        "fee_bps":      pool.fee_bps,
        "spot_price":   spot_price,   # token1 per token0
        "block":        block_num,    # 所有池的快照對應同一個 block
    }


# ──────────────────────────────────────────────
# 3. DB 層：建 pool_snapshots 表 + 寫入
# ──────────────────────────────────────────────

DDL_POOL_SNAPSHOTS = """
CREATE TABLE IF NOT EXISTS pool_snapshots (
    id          INTEGER,
    ts          TIMESTAMP,
    block       BIGINT,
    chain       VARCHAR,
    dex         VARCHAR,
    name        VARCHAR,
    pool_addr   VARCHAR,
    token0_sym  VARCHAR,
    token1_sym  VARCHAR,
    reserve0    DOUBLE,      -- human-readable (e.g. USDC amount)
    reserve1    DOUBLE,      -- human-readable (e.g. WETH amount)
    reserve0_raw HUGEINT,    -- raw uint112 (too big for BIGINT)
    reserve1_raw HUGEINT,
    fee_bps     INTEGER,
    spot_price  DOUBLE       -- token1 per token0
)
"""

SEQ_SNAPSHOTS = "CREATE SEQUENCE IF NOT EXISTS pool_snapshots_id_seq START 1"


def init_snapshots_table(conn):
    conn.execute(SEQ_SNAPSHOTS)
    conn.execute(DDL_POOL_SNAPSHOTS)
    # 設 auto-increment（DuckDB 方式）
    try:
        conn.execute("ALTER TABLE pool_snapshots ALTER id SET DEFAULT nextval('pool_snapshots_id_seq')")
    except Exception:
        pass  # 已設過


def insert_snapshot(conn, snap: dict, block: Optional[int], ts: datetime):
    conn.execute("""
        INSERT INTO pool_snapshots
            (id, ts, block, chain, dex, name, pool_addr,
             token0_sym, token1_sym,
             reserve0, reserve1, reserve0_raw, reserve1_raw,
             fee_bps, spot_price)
        VALUES (
            nextval('pool_snapshots_id_seq'),
            ?, ?, ?, ?, ?, ?,
            ?, ?,
            ?, ?, ?, ?,
            ?, ?
        )
    """, [
        ts, block, snap["chain"], snap["dex"], snap["name"], snap["pool_addr"],
        snap["token0_sym"], snap["token1_sym"],
        snap["reserve0"], snap["reserve1"], snap["reserve0_raw"], snap["reserve1_raw"],
        snap["fee_bps"], snap["spot_price"],
    ])


# ──────────────────────────────────────────────
# 4. 主程式
# ──────────────────────────────────────────────

def main():
    print("🔍 fetch_pools.py — 抓取真實池 reserves\n")

    conn = get_conn()
    init_snapshots_table(conn)

    # ② 同 block batch：先拿一次 block，所有池都傳同一個 block_hex
    block = get_block_number()
    if block is None:
        print("❌ 無法取得區塊號碼，中止")
        conn.close()
        return []
    block_hex = hex(block)  # e.g. "0x188C2A6"
    ts = datetime.now(timezone.utc)
    print(f"📦 Block: {block} ({block_hex})  |  {ts.strftime('%Y-%m-%d %H:%M:%S UTC')}\n")

    results = []
    for pool in POOLS:
        print(f"  抓取 {pool.name} ({pool.pool_addr[:10]}...)  ", end="", flush=True)
        snap = fetch_reserves(pool, block_hex=block_hex)   # ← 傳同一個 block_hex
        if snap is None:
            print("❌ 失敗")
            continue

        insert_snapshot(conn, snap, block, ts)
        results.append(snap)
        print(f"✅  {snap['token0_sym']}={snap['reserve0']:,.2f}  {snap['token1_sym']}={snap['reserve1']:,.4f}  spot={snap['spot_price']:.6f}")

    conn.commit()
    conn.close()

    print(f"\n✅ 寫入 {len(results)} 筆 → arb.duckdb / pool_snapshots  [同 block: {block}]")
    return results


if __name__ == "__main__":
    main()
