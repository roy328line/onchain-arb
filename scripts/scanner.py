"""
scanner.py — DEX 套利機會掃描器

流程：
  1. 呼叫 fetch_pools.py 抓最新 reserves（存 DB）
  2. 把 USDC/WETH 對的兩個池餵進 ev_model.best_ev()
  3. 輸出 go/no-go 決策表

執行方式：
    cd /home/ubuntu/onchain-arb
    python3 scripts/scanner.py

        # 持續掃描（每 30 秒）
    python3 scripts/scanner.py --loop --interval 30
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.fetch_pools import POOLS, fetch_reserves, get_block_number
from models.ev_model import PoolState, ChainParams, HoldingParams, best_ev

# ──────────────────────────────────────────────
# 1. 套利對設定
# ──────────────────────────────────────────────

# 每個 ARB_PAIR 定義一個「跨 DEX 同幣對」的套利機會
# pool_a_name / pool_b_name 對應 POOLS 裡的 name
ARB_PAIRS = [
    {
        "label":      "USDC/WETH: Uniswap v2 ↔ Sushiswap",
        "pool_a":     "uni_v2_usdc_weth",
        "pool_b":     "sushi_usdc_weth",
        # USDC 是 token0，WETH 是 token1
        # ev_model 的 PoolState(x, y)：x = 輸入 token，y = 輸出 token
        # 我們設：x = USDC reserve，y = WETH reserve
        "x_sym":      "USDC",
        "y_sym":      "WETH",
        "x_is_token": "token0",  # USDC 是 token0
    },
]

# ──────────────────────────────────────────────
# 2. 鏈上成本設定（L1 Ethereum）
# ──────────────────────────────────────────────

# ⚠️ 這些是估算值，需要根據當時 gas price 調整
# Day 3：先用固定值；Day 5 起可接 gas price API 動態更新
CHAIN = ChainParams(
    base_gas_usd=4.0,       # base fee（Gwei × 21-200k gas × ETH price 估算）
    priority_fee_usd=1.5,   # tip
    revert_gas_usd=1.0,     # revert 時約 21k gas
    bridge_fee_usd=0.0,     # 同鏈，無 bridge
    n_attempts=3,
    venue="public",         # 先用 public mempool；Day 9 改 bundle
)


# ──────────────────────────────────────────────
# 3. 掃描單一套利對
# ──────────────────────────────────────────────

def scan_pair(pair: dict, pool_map: dict) -> dict:
    """
    對一個 ARB_PAIR 計算套利機會。

    pool_map: { pool_name -> snap dict }  (fetch_reserves 的輸出)
    回傳結果 dict，包含 go/no-go 決策。
    """
    snap_a = pool_map.get(pair["pool_a"])
    snap_b = pool_map.get(pair["pool_b"])

    if snap_a is None or snap_b is None:
        return {
            "label":    pair["label"],
            "status":   "fetch_failed",
            "decision": "no-go",
            "reason":   f"missing: {pair['pool_a'] if not snap_a else pair['pool_b']}",
        }

    # 建 PoolState
    # simulate_arb 的約定：
    #   pool_a: x=token0 (我們投入), y=token1 (買出)
    #   pool_b: x=token1 (我們投入), y=token0 (買出) ← 方向與 pool_a 相反！
    x_key = "reserve0" if pair["x_is_token"] == "token0" else "reserve1"
    y_key = "reserve1" if pair["x_is_token"] == "token0" else "reserve0"

    pool_a = PoolState(
        x=snap_a[x_key],   # token0 reserve (e.g. USDC)
        y=snap_a[y_key],   # token1 reserve (e.g. WETH)
        fee=snap_a["fee_bps"] / 10_000,
    )
    pool_b = PoolState(
        x=snap_b[y_key],   # token1 reserve (e.g. WETH) ← 輸入方向翻轉
        y=snap_b[x_key],   # token0 reserve (e.g. USDC) ← 輸出方向翻轉
        fee=snap_b["fee_bps"] / 10_000,
    )

    # 計算 EV
    # Q_bounds[1]：pool_b 能輸入的 token1 上限（換算回 token0 計價）
    # spot_price = token1 per token0，token1 → token0 = pool_b.x / sp
    sp = snap_a["spot_price"] if snap_a["spot_price"] > 0 else 1.0
    pool_b_token0_cap = pool_b.x / sp   # pool_b 的 WETH 換算成 USDC 上限
    pool_a_token0_cap = pool_a.x        # pool_a 的 USDC 上限
    max_q = min(pool_a_token0_cap, pool_b_token0_cap) * 0.20
    max_q = max(max_q, 10.0)
    result = best_ev(pool_a, pool_b, CHAIN, Q_bounds=(1.0, max_q))

    # 現貨價差（用來判斷方向是否合理）
    spot_a = snap_a["spot_price"]  # token1 per token0
    spot_b = snap_b["spot_price"]
    spread_bps = abs(spot_a - spot_b) / max(spot_a, spot_b) * 10_000

    return {
        "label":      pair["label"],
        "status":     "ok",
        "decision":   result["decision"],
        "direction":  result["direction"],
        "Q_star":     result["Q_star"],
        "r_star":     result["r_star"],
        "ev_star":    result["ev_star"],
        "ev_realized":result["ev_realized"],
        "p_win":      result["detail"]["p_win"],
        "net_raw":    result["detail"]["net_raw"],
        "surplus":    result["detail"]["surplus_for_bribe"],
        "gas_cost":   result["detail"]["gas_cost"],
        "bribe_usd":  result["detail"]["bribe_usd"],
        "spot_a":     round(spot_a, 6),
        "spot_b":     round(spot_b, 6),
        "spread_bps": round(spread_bps, 2),
        "pool_a":     pair["pool_a"],
        "pool_b":     pair["pool_b"],
    }


# ──────────────────────────────────────────────
# 4. 格式化輸出
# ──────────────────────────────────────────────

def print_header(block, ts):
    t = ts.strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"\n{'='*64}")
    print(f"  onchain-arb scanner | block {block} | {t}")
    print(f"{'='*64}")


def print_result(r: dict):
    decision_icon = "🟢 GO    " if r["decision"] == "go" else "🔴 NO-GO"
    print(f"\n{decision_icon}  {r['label']}")

    if r["status"] != "ok":
        print(f"  ⚠️  {r['reason']}")
        return

    q_display = f"${r['Q_star']:,.0f}" if r["Q_star"] > 0 else "0（不交易）"
    print(f"  方向      : {r['direction']}")
    print(f"  最優規模  : {q_display}")
    print(f"  EV*       : ${r['ev_star']:+.4f}  （排序用，可為負）")
    print(f"  EV 實現   : ${r['ev_realized']:+.4f}  （max(0, EV*)，不交易=0）")
    print(f"  淨毛利    : ${r['net_raw']:+.4f}")
    print(f"  Surplus   : ${r['surplus']:.4f}  (bribe 上界)")
    print(f"  Gas 成本  : ${r['gas_cost']:.2f}")
    print(f"  Bribe     : ${r['bribe_usd']:.4f}  (ratio={r['r_star']:.2%})")
    print(f"  p_win     : {r['p_win']:.2%}  ⚠️ sigmoid 未校準")
    print(f"  現貨價差  : {r['spread_bps']:.2f} bps  "
          f"({r['pool_a']} {r['spot_a']}  vs  {r['pool_b']} {r['spot_b']})")


def print_summary(results: list[dict]):
    go_count = sum(1 for r in results if r["decision"] == "go")
    print(f"\n{'─'*64}")
    print(f"  掃描完成：{len(results)} 對  |  GO: {go_count}  NO-GO: {len(results) - go_count}")
    if go_count > 0:
        top = max((r for r in results if r["decision"] == "go"), key=lambda r: r.get("ev_star", -999))
        print(f"  最佳機會：{top['label']}  EV=${top['ev_star']:+.4f}")
    print(f"{'─'*64}\n")


# ──────────────────────────────────────────────
# 5. 主掃描迴圈
# ──────────────────────────────────────────────

def run_once() -> list[dict]:
    # 建 pool_name → config 的 lookup
    pool_cfg_map = {p.name: p for p in POOLS}

    # 抓所有需要的池
    needed = set()
    for pair in ARB_PAIRS:
        needed.add(pair["pool_a"])
        needed.add(pair["pool_b"])

    # ② 同 block batch：先拿一次 block，所有池傳同一個 block_hex
    block = get_block_number()
    if block is None:
        print("❌ 無法取得區塊號碼，中止", file=sys.stderr)
        return []
    block_hex = hex(block)

    pool_map = {}
    for name in needed:
        cfg = pool_cfg_map.get(name)
        if cfg is None:
            print(f"⚠️  {name} 不在 POOLS 清單", file=sys.stderr)
            continue
        snap = fetch_reserves(cfg, block_hex=block_hex)   # ← 同一個 block_hex
        if snap:
            pool_map[name] = snap
        else:
            print(f"❌ 無法抓取 {name}", file=sys.stderr)

    # ② block 一致性 assert：拒絕跨 block 的快照組進入 EV 計算
    snap_blocks = {name: s.get("block") for name, s in pool_map.items()}
    unique_blocks = set(b for b in snap_blocks.values() if b is not None)
    if len(unique_blocks) > 1:
        raise ValueError(
            f"跨 block 快照，拒絕計算 — 各池 block: {snap_blocks}\n"
            f"這代表抓取期間有新區塊產生，價差可能是時間造成的幻覺。"
        )

    ts = datetime.now(timezone.utc)
    print_header(block, ts)

    results = []
    for pair in ARB_PAIRS:
        r = scan_pair(pair, pool_map)
        print_result(r)
        results.append(r)

    print_summary(results)
    return results


def main():
    parser = argparse.ArgumentParser(description="onchain-arb scanner")
    parser.add_argument("--loop", action="store_true", help="持續掃描")
    parser.add_argument("--interval", type=int, default=30, help="掃描間隔秒數（預設 30）")
    args = parser.parse_args()

    if args.loop:
        print(f"🔄 持續掃描模式，間隔 {args.interval} 秒。Ctrl+C 停止。")
        try:
            while True:
                run_once()
                print(f"  💤 等待 {args.interval} 秒...")
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\n⛔ 掃描停止。")
    else:
        run_once()


if __name__ == "__main__":
    main()
