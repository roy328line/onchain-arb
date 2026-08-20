"""
scripts/gas_monitor.py — Day 15

Real-time Ethereum basefee 查詢 + 三角套利的動態 gas_cost 計算。

背景（2026-08-20 發現）：
  - 目前 Ethereum basefee = 0.058 gwei（100% block 低於 3 gwei）
  - Dencun 升級（2024-03-13）後 basefee 結構性降低
  - 三角套利 break-even gas = $1.05（Day 13 計算）
  - break-even gwei = $1.05 / (210_000 * 2500 * 1e-9) = 2.0 gwei
  - 現在 0.058 gwei 遠低於門檻 → 三角套利在 L1 上理論可行！

用法：
  from scripts.gas_monitor import get_gas_cost_usd, GasInfo
  info = get_gas_cost_usd()
  print(f"basefee={info.basefee_gwei:.3f} gwei, gas_cost=${info.gas_cost_usd:.4f}")
"""

import json
import urllib.request
from dataclasses import dataclass

# ── 常數 ────────────────────────────────────────────────────────────
_ETH_RPC = "https://ethereum.publicnode.com"
_HEADERS  = {"User-Agent": "curl/7.88.1", "Content-Type": "application/json"}

ETH_PRICE_USD     = 2500.0   # 靜態估算，可從外部注入
TRI_GAS_UNITS     = 210_000  # 三角套利（3 swap + overhead）預估 gas 用量
PRIORITY_FEE_GWEI = 0.1      # 優先費（tip），保守估計 0.1 gwei


@dataclass
class GasInfo:
    basefee_gwei:    float
    priority_gwei:   float
    total_gwei:      float
    gas_cost_usd:    float   # TRI_GAS_UNITS × total_gwei × ETH_PRICE
    break_even_gwei: float   # 讓 net_real=-$1.05 變正 EV 的最高 basefee
    is_below_break:  bool    # 目前 basefee 是否低於 break-even


def get_basefee_gwei(rpc: str = _ETH_RPC) -> float:
    """從最新 block 取得 baseFeePerGas（gwei）。"""
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": "eth_getBlockByNumber",
        "params": ["latest", False]
    }).encode()
    req  = urllib.request.Request(rpc, data=body, headers=_HEADERS)
    resp = json.loads(urllib.request.urlopen(req, timeout=8).read())
    base_hex = resp["result"]["baseFeePerGas"]
    return int(base_hex, 16) / 1e9  # wei → gwei


def get_gas_cost_usd(
    eth_price: float = ETH_PRICE_USD,
    gas_units: int   = TRI_GAS_UNITS,
    priority_gwei: float = PRIORITY_FEE_GWEI,
    rpc: str = _ETH_RPC,
) -> GasInfo:
    """
    查 real-time basefee，計算三角套利的動態 gas_cost。

    gas_cost_usd = (basefee + priority) × gas_units × eth_price × 1e-9
    break_even_gwei = net_real_loss / (gas_units × eth_price × 1e-9)
                    ← 「把虧損轉成 gwei 門檻」
    """
    basefee  = get_basefee_gwei(rpc)
    total    = basefee + priority_gwei
    cost_usd = total * gas_units * eth_price * 1e-9

    # break-even：讓 DAI→WETH→USDT 的 net_real=-$1.05 剛好變正 EV
    # net_ev = net_real - gas_cost_usd > 0
    # → gas_cost_usd < |net_real| = 1.05
    # → total_gwei < 1.05 / (gas_units × eth_price × 1e-9)
    NET_REAL_DAI_WETH_USDT = 1.05  # Day 13 Quoter 實測（固定值，待更新）
    break_even = NET_REAL_DAI_WETH_USDT / (gas_units * eth_price * 1e-9)

    return GasInfo(
        basefee_gwei    = round(basefee, 4),
        priority_gwei   = priority_gwei,
        total_gwei      = round(total, 4),
        gas_cost_usd    = round(cost_usd, 4),
        break_even_gwei = round(break_even, 2),
        is_below_break  = total < break_even,
    )


def gas_status_line(info: GasInfo) -> str:
    flag = "✅ EV+" if info.is_below_break else "❌ EV-"
    return (f"{flag}  basefee={info.basefee_gwei:.3f} gwei  "
            f"gas_cost=${info.gas_cost_usd:.4f}  "
            f"break_even={info.break_even_gwei:.2f} gwei")


if __name__ == "__main__":
    print("⛽ Ethereum Gas Monitor")
    print(f"  ETH price assumption: ${ETH_PRICE_USD:,}")
    print(f"  Tri-arb gas units:    {TRI_GAS_UNITS:,}")
    print()
    try:
        info = get_gas_cost_usd()
        print(f"  basefee:      {info.basefee_gwei:.4f} gwei")
        print(f"  priority tip: {info.priority_gwei:.4f} gwei")
        print(f"  total:        {info.total_gwei:.4f} gwei")
        print(f"  gas_cost_usd: ${info.gas_cost_usd:.4f}")
        print()
        print(f"  break-even gwei: {info.break_even_gwei:.2f}")
        print(f"  {'✅ 低於 break-even！DAI→WETH→USDT 可能正 EV' if info.is_below_break else '❌ 高於 break-even'}")
        print()

        # 不同 ETH 價格假設下的 gas_cost
        print("  ── 敏感度分析（不同 ETH 價格）──")
        print(f"  {'ETH 價格':>10}  {'gas_cost':>12}  {'break-even gwei':>17}  狀態")
        for price in [1500, 2000, 2500, 3000, 4000]:
            cost = info.total_gwei * TRI_GAS_UNITS * price * 1e-9
            be   = info.NET_REAL_DAI_WETH_USDT if hasattr(info, 'NET_REAL_DAI_WETH_USDT') else 1.05
            be_g = be / (TRI_GAS_UNITS * price * 1e-9)
            ok   = "✅" if info.total_gwei < be_g else "❌"
            print(f"  ${price:>9,}  ${cost:>11.4f}  {be_g:>17.2f}  {ok}")
    except Exception as e:
        print(f"  ❌ 錯誤: {e}")
