"""
Day 14 — Arbitrum Quoter 驗證腳本

目標：驗證 DAI→WETH→USDT（及 DAI→USDC→USDT）在 Arbitrum 上是否正 EV。

Arbitrum 合約地址（與 Ethereum mainnet 不同）：
  - UniswapV3 Quoter V1：0xb27308f9F90D607463bb33eA1BeBb41C27CE5AB6（同 mainnet）
  - UniswapV3 QuoterV2 ：0x61fFE014bA17989E743c5F6cB21bF9697530B21e（Arb only）
  - WETH ：0x82aF49447D8a07e3bd95BD0d56f35241523fBab1
  - USDC.e：0xFF970A61A04b1cA14834A43f5dE4533eBDDB5CC8
  - USDC  ：0xaf88d065e77c8cC2239327C5EDb3A432268e5831
  - USDT  ：0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9
  - DAI   ：0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1
"""

import json
import urllib.request as _ur

# ── Arbitrum 設定 ─────────────────────────────────────────────────
_ARB_RPC     = "https://arbitrum.publicnode.com"
_QUOTER_V3   = "0xb27308f9F90D607463bb33eA1BeBb41C27CE5AB6"  # Uniswap V3 Quoter（同 mainnet）
_HEADERS     = {"User-Agent": "curl/7.88.1", "Content-Type": "application/json"}

# Arbitrum token 地址
WETH  = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"
DAI   = "0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1"
USDT  = "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9"
USDC  = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
USDCe = "0xFF970A61A04b1cA14834A43f5dE4533eBDDB5CC8"

# decimals
DECIMALS = {
    WETH:  18,
    DAI:   18,
    USDT:   6,
    USDC:   6,
    USDCe:  6,
}

# Arbitrum gas 估計（三角 tx，含 calldata overhead）
# 目前 Arbitrum basefee ~0.01-0.1 gwei，估計保守值
ARB_GAS_ESTIMATE = 0.20   # $0.20 USD（保守估計）
ARB_GAS_OPTIMISTIC = 0.05  # $0.05 USD（低費用時段）

# USD 價格（用於 DAI/USDT/USDC，≈1；WETH 由市場決定）
TOKEN_USD = {DAI: 1.0, USDT: 1.0, USDC: 1.0, USDCe: 1.0, WETH: 2500.0}


def _p32(v: int) -> str:
    return format(int(v), "064x")


def _pa(addr: str) -> str:
    return _p32(int(addr, 16))


def eth_call(to: str, data: str, rpc: str = _ARB_RPC) -> str:
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "eth_call",
        "params": [{"to": to, "data": data}, "latest"]
    }).encode()
    req = _ur.Request(rpc, data=body, headers=_HEADERS)
    resp = json.loads(_ur.urlopen(req, timeout=10).read())
    if "error" in resp:
        raise RuntimeError(f"eth_call error: {resp['error']}")
    return resp["result"]


def quoter_single(token_in: str, token_out: str, fee: int, amount_in_wei: int) -> int | None:
    """
    UniswapV3Quoter.quoteExactInputSingle on Arbitrum。
    fee 單位：pips（100=0.01%, 500=0.05%, 3000=0.3%）。
    """
    try:
        cd = ("0xf7729d43"
              + _pa(token_in)
              + _pa(token_out)
              + _p32(fee)
              + _p32(amount_in_wei)
              + _p32(0))
        res = eth_call(_QUOTER_V3, cd)
        return int(res, 16)
    except Exception as e:
        print(f"  [Quoter fail] {token_in[:8]}→{token_out[:8]} fee={fee}: {e}")
        return None


def to_wei(amount: float, token: str) -> int:
    return int(amount * 10 ** DECIMALS[token])


def from_wei(amount_wei: int, token: str) -> float:
    return amount_wei / 10 ** DECIMALS[token]


def check_rpc() -> bool:
    """確認 Arbitrum RPC 連通"""
    try:
        body = json.dumps({"jsonrpc":"2.0","id":1,"method":"eth_blockNumber","params":[]}).encode()
        req  = _ur.Request(_ARB_RPC, data=body, headers=_HEADERS)
        resp = json.loads(_ur.urlopen(req, timeout=8).read())
        block = int(resp["result"], 16)
        print(f"✅ Arbitrum RPC 連通，最新 block: {block:,}")
        return True
    except Exception as e:
        print(f"❌ Arbitrum RPC 失敗: {e}")
        return False


def scan_path(name: str, token_a: str, token_b: str, token_c: str,
              fee_ab: int, fee_bc: int,
              q_values: list[float]) -> list[dict]:
    """
    掃描一條三角路徑在不同 Q* 下的 net_real。
    """
    sym_a = [k for k,v in {"DAI":DAI,"USDT":USDT,"USDC":USDC,"WETH":WETH,"USDCe":USDCe}.items() if v==token_a][0]
    sym_b = [k for k,v in {"DAI":DAI,"USDT":USDT,"USDC":USDC,"WETH":WETH,"USDCe":USDCe}.items() if v==token_b][0]
    sym_c = [k for k,v in {"DAI":DAI,"USDT":USDT,"USDC":USDC,"WETH":WETH,"USDCe":USDCe}.items() if v==token_c][0]
    print(f"\n{'='*60}")
    print(f"路徑：{sym_a}→{sym_b}→{sym_c}→{sym_a}  fee={fee_ab}/{fee_bc}")
    print(f"{'='*60}")
    print(f"{'Q_star':>10}  {'Q_out':>10}  {'net_raw':>10}  {'net_arb($0.20)':>15}  {'net_arb($0.05)':>15}")

    results = []
    for q in q_values:
        q_wei = to_wei(q, token_a)
        price_a = TOKEN_USD[token_a]

        # Leg 1: token_a → token_b
        w_wei = quoter_single(token_a, token_b, fee_ab, q_wei)
        if w_wei is None:
            continue

        # Leg 2: token_b → token_c
        v_wei = quoter_single(token_b, token_c, fee_bc, w_wei)
        if v_wei is None:
            continue

        # Leg 3: token_c → token_a（用各 fee tier 都試，取最好的）
        best_out = None
        best_fee = None
        for fee_ca in [100, 500, 3000]:
            out = quoter_single(token_c, token_a, fee_ca, v_wei)
            if out is not None and (best_out is None or out > best_out):
                best_out = out
                best_fee = fee_ca

        if best_out is None:
            print(f"  Q={q:>8.1f}  Leg3 全失敗")
            continue

        q_out    = from_wei(best_out, token_a)
        net_raw  = (q_out - q) * price_a
        net_0_20 = net_raw - ARB_GAS_ESTIMATE
        net_0_05 = net_raw - ARB_GAS_OPTIMISTIC

        flag = "✅" if net_0_20 > 0 else ("🔶" if net_raw > 0 else "  ")
        print(f"  {q:>10.2f}  {q_out:>10.4f}  {net_raw:>+10.4f}  {net_0_20:>+15.4f}  {net_0_05:>+15.4f}  {flag}  fee_ca={best_fee}")
        results.append({
            "q_star": q, "q_out": q_out, "net_raw": net_raw,
            "net_at_020": net_0_20, "net_at_005": net_0_05,
            "fee_ca": best_fee,
        })

    return results


if __name__ == "__main__":
    print("🔺 Arbitrum Quoter 驗證 — Day 14")
    print()

    if not check_rpc():
        exit(1)

    # Ethereum mainnet 的 Q* 參考值（Day 13 Quoter 驗出）
    # DAI→WETH→USDT: Q*=657.6 DAI（net_real=-$1.05 on ETH）
    # DAI→USDC→USDT: Q*=177.8 / 827.8 DAI

    # ① DAI→WETH→USDT（Arbitrum）
    # UniV3 DAI/WETH fee=3000（0.3%）或 500（0.05%）
    # UniV3 WETH/USDT fee=500（0.05%）
    for fee_ab in [3000, 500]:
        scan_path(
            name="DAI→WETH→USDT",
            token_a=DAI, token_b=WETH, token_c=USDT,
            fee_ab=fee_ab, fee_bc=500,
            q_values=[10, 50, 100, 200, 500, 657, 1000, 2000],
        )

    # ② DAI→USDC→USDT（Arbitrum）
    # 穩定幣池通常 fee=100（0.01%）或 500（0.05%）
    for fee_ab in [100, 500]:
        scan_path(
            name="DAI→USDC→USDT",
            token_a=DAI, token_b=USDC, token_c=USDT,
            fee_ab=fee_ab, fee_bc=100,
            q_values=[50, 100, 177, 500, 827, 1000, 2000, 5000],
        )

    # ③ DAI→USDCe→USDT（舊版 USDC.e，流動性可能更深）
    scan_path(
        name="DAI→USDCe→USDT",
        token_a=DAI, token_b=USDCe, token_c=USDT,
        fee_ab=500, fee_bc=100,
        q_values=[50, 100, 177, 500, 827, 1000],
    )

    print("\n" + "="*60)
    print("完成。")
