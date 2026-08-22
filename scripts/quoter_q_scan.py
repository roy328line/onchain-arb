"""
scripts/quoter_q_scan.py — Day 17（P0）

問題：為什麼 net_star（AMM 模型）是正的，但 net_real（Quoter 驗證）是負的？

調查方法：對 DB 中 net_real 最佳的幾條路徑，
用 Quoter 從極小 Q（$1）掃到大 Q（$2000），
畫出 net_real(Q) 曲線，找 break-even 臨界點。

如果 Q→0 時 net_real 仍是負的：路徑本身就沒有機會
如果 Q=1 時 net_real > 0：存在機會，只是 Q_star 估算過大

使用方式：
  python3 scripts/quoter_q_scan.py
"""

import sys
import os
import json
import urllib.request as _ur
from dataclasses import dataclass

# ── 路徑設定 ──────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

# ── RPC / Contract 常數（Ethereum mainnet）────────────
_ETH_RPC     = "https://ethereum.publicnode.com"
_RPC_HEADERS = {"User-Agent": "curl/7.88.1", "Content-Type": "application/json"}
_ROUTER_V2   = "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D"
_QUOTER_V3   = "0xb27308f9F90D607463bb33eA1BeBb41C27CE5AB6"

TOKEN_DECIMALS = {
    "0x6b175474e89094c44da98b954eedeac495271d0f": 18,  # DAI
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": 18,  # WETH
    "0xdac17f958d2ee523a2206206994597c13d831ec7": 6,   # USDT
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": 6,   # USDC
    "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599": 8,   # WBTC
    "0xe76c6c83af64e4c60245d8c7de953df673a7a33d": 18,  # RAIL
}

TOKEN_USD_PRICE = {
    "0x6b175474e89094c44da98b954eedeac495271d0f": 1.0,      # DAI
    "0xdac17f958d2ee523a2206206994597c13d831ec7": 1.0,      # USDT
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": 1.0,      # USDC
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": 2400.0,   # WETH（近似）
    "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599": 60000.0,  # WBTC（近似）
}

# ── DEX 費率對照（從 DB dex 名稱反推 fee pips）────────
DEX_FEE_PIPS = {
    "uniswap_v3":   500,    # 預設 0.05%
    "sushiswap_v3": 500,
    "uniswap_v2":   0,      # v2 不用 pips
    "sushiswap_v2": 0,
}


# ── eth_call 工具 ─────────────────────────────────────
def _p32(v: int) -> str:
    return format(int(v), "064x")

def _pa(addr: str) -> str:
    return _p32(int(addr, 16))

def _eth_call(to: str, data: str) -> str:
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "eth_call",
        "params": [{"to": to, "data": data}, "latest"]
    }).encode()
    req  = _ur.Request(_ETH_RPC, data=body, headers=_RPC_HEADERS)
    resp = json.loads(_ur.urlopen(req, timeout=10).read())
    if "error" in resp:
        raise RuntimeError(f"eth_call error: {resp['error']}")
    return resp["result"]


def v2_quote(token_in: str, token_out: str, amount_in_wei: int) -> int:
    cd = ("0xd06ca61f"
          + _p32(amount_in_wei)
          + _p32(0x40)
          + _p32(2)
          + _pa(token_in)
          + _pa(token_out))
    res = _eth_call(_ROUTER_V2, cd)
    raw = bytes.fromhex(res.removeprefix("0x"))
    # getAmountsOut 回傳 (uint offset, uint count, uint[], uint[])
    off = int.from_bytes(raw[:32], "big")
    cnt = int.from_bytes(raw[off:off+32], "big")
    amounts = [
        int.from_bytes(raw[off+32 + i*32 : off+64 + i*32], "big")
        for i in range(cnt)
    ]
    return amounts[1]  # amount_out


def v3_quote(token_in: str, token_out: str, fee_pips: int, amount_in_wei: int) -> int:
    cd = ("0xf7729d43"
          + _pa(token_in)
          + _pa(token_out)
          + _p32(fee_pips)
          + _p32(amount_in_wei)
          + _p32(0))
    res = _eth_call(_QUOTER_V3, cd)
    return int(res, 16)


def three_leg_quoter(
    tok_a: str, tok_b: str, tok_c: str,
    dex_ab: str, dex_bc: str, dex_ca: str,
    Q_usd: float,
) -> float:
    """
    三腿串接 Quoter：A→B→C→A，回傳 net_real_usd。

    dex 名稱判斷：含 'v3' → v3_quote，否則 v2_quote。
    v3 fee_pips 從 DEX_FEE_PIPS 查，查不到用 500（0.05%）。
    """
    d_a = TOKEN_DECIMALS.get(tok_a, 18)
    d_b = TOKEN_DECIMALS.get(tok_b, 18)
    d_c = TOKEN_DECIMALS.get(tok_c, 18)
    price_a = TOKEN_USD_PRICE.get(tok_a, 1.0)

    q_in_wei = int(Q_usd / price_a * (10 ** d_a))

    try:
        # Leg 1: A → B
        if "v3" in dex_ab:
            fee1 = DEX_FEE_PIPS.get(dex_ab, 500)
            b_wei = v3_quote(tok_a, tok_b, fee1, q_in_wei)
        else:
            b_wei = v2_quote(tok_a, tok_b, q_in_wei)

        # Leg 2: B → C
        if "v3" in dex_bc:
            fee2 = DEX_FEE_PIPS.get(dex_bc, 500)
            c_wei = v3_quote(tok_b, tok_c, fee2, b_wei)
        else:
            c_wei = v2_quote(tok_b, tok_c, b_wei)

        # Leg 3: C → A
        if "v3" in dex_ca:
            fee3 = DEX_FEE_PIPS.get(dex_ca, 500)
            a_out_wei = v3_quote(tok_c, tok_a, fee3, c_wei)
        else:
            a_out_wei = v2_quote(tok_c, tok_a, c_wei)

    except Exception as e:
        return float("nan"), str(e)

    q_out = a_out_wei / (10 ** d_a)
    net_real_usd = (q_out - Q_usd / price_a) * price_a
    return net_real_usd, None


# ── 要掃描的路徑（從 DB top paths 取）────────────────
SCAN_PATHS = [
    # 最佳路徑（DB net_real 最接近 0）
    {
        "name": "DAI→WETH→USDT (v2/sushi_v2/v3)",
        "tok_a": "0x6b175474e89094c44da98b954eedeac495271d0f",
        "tok_b": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
        "tok_c": "0xdac17f958d2ee523a2206206994597c13d831ec7",
        "dex_ab": "uniswap_v2",
        "dex_bc": "sushiswap_v2",
        "dex_ca": "uniswap_v3",
    },
    # 第二條路徑（全 v3）
    {
        "name": "DAI→WETH→USDT (v3/v3/v3)",
        "tok_a": "0x6b175474e89094c44da98b954eedeac495271d0f",
        "tok_b": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
        "tok_c": "0xdac17f958d2ee523a2206206994597c13d831ec7",
        "dex_ab": "uniswap_v3",
        "dex_bc": "uniswap_v3",
        "dex_ca": "uniswap_v3",
    },
    # 反向：WETH→DAI→USDT（不同起點）
    {
        "name": "DAI→USDT→WETH (v3 last leg)",
        "tok_a": "0x6b175474e89094c44da98b954eedeac495271d0f",
        "tok_b": "0xdac17f958d2ee523a2206206994597c13d831ec7",
        "tok_c": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
        "dex_ab": "uniswap_v3",
        "dex_bc": "uniswap_v3",
        "dex_ca": "uniswap_v3",
    },
]

# Q 掃描區間（USD）
Q_SWEEP = [1, 5, 10, 20, 50, 100, 200, 400, 600, 800, 1000, 1500, 2000]


def main():
    print("=" * 70)
    print("  Day 17 Quoter Q 掃描 — 找 net_real=0 臨界點")
    print("=" * 70)

    for path in SCAN_PATHS:
        print(f"\n{'─'*70}")
        print(f"路徑：{path['name']}")
        print(f"  A={path['tok_a'][:10]}… dex_ab={path['dex_ab']}")
        print(f"  B={path['tok_b'][:10]}… dex_bc={path['dex_bc']}")
        print(f"  C={path['tok_c'][:10]}… dex_ca={path['dex_ca']}")
        print(f"{'─'*70}")
        print(f"  {'Q_usd':>8}  {'net_real_usd':>14}  {'ROI%':>7}  狀態")
        print(f"  {'-'*8}  {'-'*14}  {'-'*7}  ----")

        best_q = None
        best_net = float("-inf")

        for q in Q_SWEEP:
            net, err = three_leg_quoter(
                path["tok_a"], path["tok_b"], path["tok_c"],
                path["dex_ab"], path["dex_bc"], path["dex_ca"],
                q,
            )
            if err:
                print(f"  {q:>8.0f}  {'ERROR':>14}  {'':>7}  {err[:40]}")
                continue

            roi = net / q * 100 if q else 0
            tag = "✅ 正 EV！" if net > 0 else ("🔶 接近" if net > -0.5 else "❌")
            print(f"  {q:>8.0f}  {net:>+14.4f}  {roi:>+7.4f}%  {tag}")

            if net > best_net:
                best_net = net
                best_q   = q

        print(f"\n  → 最佳 Q={best_q}, net_real={best_net:+.4f}")
        if best_net > 0:
            print(f"  → ✅ 此路徑在 Q=${best_q} 時有真實機會！")
        else:
            print(f"  → ❌ 所有 Q 均為負，此路徑方向無機會")

    print(f"\n{'='*70}")
    print("  掃描完成")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()
