"""
db.py — DuckDB 連線工具
用法：from scripts.db import get_conn
"""
import duckdb
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "arb.duckdb"


def get_conn(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """回傳一個連到 data/arb.duckdb 的連線。"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(DB_PATH), read_only=read_only)
