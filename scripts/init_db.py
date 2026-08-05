"""
init_db.py — 建立 arb.duckdb 的四張核心表

執行方式：
    python3 scripts/init_db.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from scripts.db import get_conn

DDL = {
    "pools": """
        CREATE TABLE IF NOT EXISTS pools (
            id          INTEGER PRIMARY KEY,   -- auto surrogate key
            chain       VARCHAR,               -- e.g. 'ethereum', 'arbitrum'
            dex         VARCHAR,               -- e.g. 'uniswap_v3', 'camelot'
            pool_addr   VARCHAR,               -- 0x...
            token0      VARCHAR,               -- 0x...
            token1      VARCHAR,               -- 0x...
            reserve0    DOUBLE,                -- raw reserve of token0
            reserve1    DOUBLE,                -- raw reserve of token1
            fee_tier    INTEGER,               -- bps * 100，e.g. 3000 = 0.3%
            block       BIGINT,
            ts          TIMESTAMP DEFAULT now()
        )
    """,

    "quotes": """
        CREATE TABLE IF NOT EXISTS quotes (
            id              INTEGER PRIMARY KEY,
            src             VARCHAR,           -- api source, e.g. 'lifi', '1inch', 'paraswap'
            chain_from      VARCHAR,
            chain_to        VARCHAR,
            token_in        VARCHAR,
            token_out       VARCHAR,
            amount_in       DOUBLE,
            amount_out      DOUBLE,
            amount_out_min  DOUBLE,
            gas_usd         DOUBLE,
            fee_usd         DOUBLE,
            -- 重要：LI.FI feeCosts[].included=true 代表費用已扣在 fromAmount 裡。
            -- 若 fee_included=true，不要再疊加 fee_usd，否則重複計算。
            fee_included    BOOLEAN DEFAULT false,
            tool            VARCHAR,           -- 實際使用的 route/tool
            duration_s      DOUBLE,            -- api round-trip 秒數
            raw_json        JSON,              -- 原始回應，備查
            ts              TIMESTAMP DEFAULT now()
        )
    """,

    "opportunities": """
        CREATE TABLE IF NOT EXISTS opportunities (
            id                  INTEGER PRIMARY KEY,
            strategy            VARCHAR,       -- e.g. 'dex_arb', 'cross_chain_arb'
            description         VARCHAR,
            gross_usd           DOUBLE,        -- 毛利（未扣成本）
            cost_breakdown_json JSON,          -- {gas, fee, slippage, ...}
            ev_usd              DOUBLE,        -- 期望值 = gross - costs
            p_win               DOUBLE,        -- 勝率估計 0~1
            decision            VARCHAR,       -- 'skip' | 'execute' | 'pending'
            ts                  TIMESTAMP DEFAULT now()
        )
    """,

    "trades": """
        CREATE TABLE IF NOT EXISTS trades (
            id          INTEGER PRIMARY KEY,
            tx_hash     VARCHAR UNIQUE,        -- 0x...（真實成交後填入）
            chain       VARCHAR,
            strategy    VARCHAR,
            expected_ev DOUBLE,
            actual_pnl  DOUBLE,               -- 實際損益（含 gas）
            gas_paid    DOUBLE,               -- USD
            status      VARCHAR,              -- 'pending' | 'confirmed' | 'failed' | 'reverted'
            notes       VARCHAR,
            ts          TIMESTAMP DEFAULT now()
        )
    """,
}

SEQUENCES = [
    "CREATE SEQUENCE IF NOT EXISTS pools_id_seq START 1",
    "CREATE SEQUENCE IF NOT EXISTS quotes_id_seq START 1",
    "CREATE SEQUENCE IF NOT EXISTS opportunities_id_seq START 1",
    "CREATE SEQUENCE IF NOT EXISTS trades_id_seq START 1",
]

ALTER_DEFAULTS = [
    "ALTER TABLE pools         ALTER id SET DEFAULT nextval('pools_id_seq')",
    "ALTER TABLE quotes        ALTER id SET DEFAULT nextval('quotes_id_seq')",
    "ALTER TABLE opportunities ALTER id SET DEFAULT nextval('opportunities_id_seq')",
    "ALTER TABLE trades        ALTER id SET DEFAULT nextval('trades_id_seq')",
]


def main():
    conn = get_conn()

    print("建立 sequences...")
    for sql in SEQUENCES:
        conn.execute(sql)

    print("建立 tables...")
    for name, ddl in DDL.items():
        conn.execute(ddl)
        print(f"  ✅ {name}")

    print("設定 auto-increment defaults...")
    for sql in ALTER_DEFAULTS:
        conn.execute(sql)

    conn.close()
    print("\n✅ init_db 完成，資料庫：data/arb.duckdb")


if __name__ == "__main__":
    main()
