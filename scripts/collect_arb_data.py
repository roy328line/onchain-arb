"""
Day 8 校準資料收集：從公開 RPC 不用 trace 估算 atomic arb bribe_ratio

方法：
  atomic arb = 同一筆 tx 內，bot 地址的某個 token 淨流入 > 0（賺到錢）
  gross_profit = bot 地址的 WETH 淨流入（最常見的利潤 token）
  bribe        = priority_fee_per_gas × gas_used（ETH tip 給 builder）
               ⚠️ 不含 coinbase.transfer，是 bribe 下限

  bribe_ratio = bribe_eth / max(0, gross_profit_eth - gas_base_eth)
"""

import json, time, math, sys
from collections import defaultdict

import requests

RPC = "https://ethereum.publicnode.com"
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
USDT = "0xdac17f958d2ee523a2206206994597c13d831ec7"

# Transfer event signature
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

def rpc(method, params):
    r = requests.post(RPC, json={"jsonrpc":"2.0","method":method,"params":params,"id":1}, timeout=10)
    return r.json().get("result")

def hex_int(h):
    if h is None: return 0
    return int(h, 16) if isinstance(h, str) else h

def get_receipts_for_block(block_hex):
    """eth_getBlockReceipts — 一次拿整個 block 所有 receipt"""
    return rpc("eth_getBlockReceipts", [block_hex])

def get_block(block_hex):
    return rpc("eth_getBlockByNumber", [block_hex, False])

def analyze_receipt(receipt, base_fee_per_gas):
    """
    從單筆 receipt 估算 atomic arb 的 gross_profit 和 bribe。
    回傳 None 如果不像是 arb。
    """
    tx_hash = receipt["transactionHash"]
    tx_from = receipt["from"].lower()
    gas_used = hex_int(receipt["gasUsed"])
    effective_gp = hex_int(receipt.get("effectiveGasPrice", "0x0"))

    # priority fee = effectiveGasPrice - baseFee
    priority_fee_per_gas = max(0, effective_gp - base_fee_per_gas)
    bribe_wei = priority_fee_per_gas * gas_used
    base_gas_wei = base_fee_per_gas * gas_used

    # 只看有多個 log 的 tx（arb 至少要有多次 swap）
    logs = receipt.get("logs", [])
    transfer_logs = [l for l in logs if
                     len(l["topics"]) >= 3 and
                     l["topics"][0].lower() == TRANSFER_TOPIC]

    if len(transfer_logs) < 3:  # 至少 3 個 transfer（2 swap 最少）
        return None

    # 計算 bot 地址的 WETH 淨流入
    weth_logs = [l for l in transfer_logs if l["address"].lower() == WETH]
    if len(weth_logs) < 2:
        return None

    # 用 from/to 計算 bot 的 WETH 淨變動
    weth_balance = defaultdict(int)
    for l in weth_logs:
        src  = "0x" + l["topics"][1][-40:]
        dst  = "0x" + l["topics"][2][-40:]
        amt  = hex_int(l["data"])
        weth_balance[src] -= amt
        weth_balance[dst] += amt

    # bot = tx_from，或 to（有些 arb 用 contract）
    tx_to = receipt.get("to", "").lower() if receipt.get("to") else ""
    candidates = [tx_from, tx_to]

    gross_weth = 0
    for addr in candidates:
        delta = weth_balance.get(addr, 0)
        if delta > 0:
            gross_weth = max(gross_weth, delta)

    if gross_weth <= 0:
        return None

    gross_eth = gross_weth / 1e18
    bribe_eth  = bribe_wei / 1e18
    base_eth   = base_gas_wei / 1e18
    surplus    = max(0, gross_eth - base_eth)

    if surplus <= 0 or bribe_eth <= 0:
        return None

    bribe_ratio = bribe_eth / surplus

    return {
        "tx_hash":      tx_hash,
        "gross_eth":    round(gross_eth, 8),
        "bribe_eth":    round(bribe_eth, 8),
        "base_eth":     round(base_eth, 8),
        "surplus":      round(surplus, 8),
        "bribe_ratio":  round(bribe_ratio, 6),
        "n_transfers":  len(transfer_logs),
        "n_weth":       len(weth_logs),
        "priority_gwei": round(priority_fee_per_gas / 1e9, 3),
    }


def collect(n_blocks=50, start_offset=0):
    """掃最近 n_blocks 個 block，收集 atomic arb 樣本。"""
    # 取最新 block number
    latest_hex = rpc("eth_blockNumber", [])
    latest = hex_int(latest_hex) - start_offset
    print(f"掃描 block {latest - n_blocks + 1} → {latest}（共 {n_blocks} 個）")

    results = []
    for i in range(n_blocks):
        blk = latest - i
        blk_hex = hex(blk)

        # 取 block header
        block = get_block(blk_hex)
        if not block:
            continue
        base_fee = hex_int(block.get("baseFeePerGas", "0x0"))

        # 取所有 receipts
        receipts = get_receipts_for_block(blk_hex)
        if not receipts:
            continue

        for r in receipts:
            arb = analyze_receipt(r, base_fee)
            if arb:
                arb["block"] = blk
                results.append(arb)

        if (i+1) % 10 == 0:
            print(f"  {i+1}/{n_blocks} blocks done, {len(results)} arbs found")
        time.sleep(0.1)  # rate limit

    return results


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 50
    data = collect(n_blocks=n)

    print(f"\n=== 收集結果：{len(data)} 筆疑似 atomic arb ===")
    if not data:
        print("沒有找到樣本，可能需要調整過濾條件")
        sys.exit(1)

    # 存 JSON
    with open("notes/day08_arb_raw.json", "w") as f:
        json.dump(data, f, indent=2)
    print("已存：notes/day08_arb_raw.json")

    # 快速統計
    ratios = [d["bribe_ratio"] for d in data if 0 < d["bribe_ratio"] <= 2]
    if ratios:
        ratios.sort()
        n = len(ratios)
        print(f"\nbribe_ratio 分布（n={n}）：")
        print(f"  p10={ratios[n//10]:.3f}  p25={ratios[n//4]:.3f}  "
              f"p50={ratios[n//2]:.3f}  p75={ratios[3*n//4]:.3f}  p90={ratios[9*n//10]:.3f}")
        print(f"  mean={sum(ratios)/n:.3f}  max={max(ratios):.3f}")
        print()
        # 桶
        buckets = {"S(WETH<0.003)":[], "M(0.003-0.015)":[], "L(0.015-0.06)":[], "XL(>0.06)":[]}
        for d in data:
            g = d["gross_eth"]
            r = d["bribe_ratio"]
            if 0 < r <= 2:
                if g < 0.003:    buckets["S(WETH<0.003)"].append(r)
                elif g < 0.015:  buckets["M(0.003-0.015)"].append(r)
                elif g < 0.06:   buckets["L(0.015-0.06)"].append(r)
                else:            buckets["XL(>0.06)"].append(r)
        print("規模桶：")
        for bname, bvals in buckets.items():
            if bvals:
                bvals.sort()
                n2 = len(bvals)
                print(f"  {bname}: n={n2} "
                      f"p50={bvals[n2//2]:.3f} p90={bvals[9*n2//10]:.3f}")
