"""
Day 02 — 真實池驗證 + 衝擊曲線
從 Uniswap v3 USDC/WETH 0.05% 池抓當前狀態，
換算成 v2 等效 x/y，畫四種交易規模的 price impact。
"""

import json, sys, math, os
import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from models.ev_model import price_impact_v2

# ── 1. 抓 Uniswap v3 USDC/WETH 0.05% 池狀態（The Graph）──
POOL_ADDRESS = "0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640"  # USDC/WETH 0.05%

GRAPH_URL = "https://gateway.thegraph.com/api/subgraphs/id/5zvR82QoaXYFyDEKLZ9t6v9adgnptxYpKpSbxtgVENFV"

# 備用：用公開 RPC 抓 slot0 + liquidity
PUBLIC_RPC = "https://eth.llamarpc.com"

def fetch_pool_via_rpc() -> dict:
    """用公開 RPC 抓 Uniswap v3 pool slot0 + liquidity。"""
    # slot0: sqrtPriceX96, tick, ...
    # liquidity: 當前活躍流動性

    def rpc(method, params):
        r = requests.post(PUBLIC_RPC, json={
            "jsonrpc": "2.0", "id": 1,
            "method": method, "params": params
        }, timeout=10)
        return r.json().get("result")

    # slot0() selector: 0x3850c7bd
    slot0_raw = rpc("eth_call", [{
        "to": POOL_ADDRESS,
        "data": "0x3850c7bd"
    }, "latest"])

    # liquidity() selector: 0x1a686502
    liq_raw = rpc("eth_call", [{
        "to": POOL_ADDRESS,
        "data": "0x1a686502"
    }, "latest"])

    if not slot0_raw or not liq_raw:
        raise RuntimeError("RPC 回傳空值")

    # decode sqrtPriceX96 (first 32 bytes = uint160)
    sqrt_price_x96 = int(slot0_raw[2:66], 16)
    liquidity = int(liq_raw[2:], 16)

    return {"sqrtPriceX96": sqrt_price_x96, "liquidity": liquidity}


def v3_to_v2_equiv(sqrtPriceX96: int, liquidity: int) -> tuple[float, float]:
    """
    將 v3 的 sqrtPriceX96 + liquidity 換算成 v2 等效儲備量。

    USDC/WETH 0.05% 池：
      token0 = USDC (decimals=6)
      token1 = WETH (decimals=18)

    sqrtPriceX96 = sqrt(token1/token0) * 2^96

    v3 等效流動性虛擬儲備（在當前 tick 附近的有效深度）：
      x = L / sqrt(P)   （token0 = USDC）
      y = L * sqrt(P)   （token1 = WETH）
    其中 sqrt(P) 已包含 decimals 調整。
    """
    Q96 = 2 ** 96
    sqrt_p = sqrtPriceX96 / Q96

    # price = (sqrtP)^2，但 token0=USDC(6 dec)、token1=WETH(18 dec)
    # 真實 ETH/USDC 價格（USDC per ETH）= 1/price * 10^(18-6)
    price_raw = sqrt_p ** 2          # token1/token0 raw
    decimal_adj = 10 ** (18 - 6)     # = 1e12
    eth_price_usdc = (1 / price_raw) * decimal_adj

    # 等效流動性（以 USDC 計價做成本模型用）
    # L 的單位是 sqrt(token0 * token1)，需要用 decimals 調整
    L = liquidity
    # virtual reserves at current price（raw token units）：
    x_raw = L / sqrt_p               # USDC raw (1e6 units)
    y_raw = L * sqrt_p               # WETH raw (1e18 units)

    x_usdc = x_raw / 1e6            # USDC 金額
    y_usdc = (y_raw / 1e18) * eth_price_usdc  # WETH → USDC

    return x_usdc, y_usdc, eth_price_usdc


# ── 2. 主程式 ──
print("抓取 Uniswap v3 USDC/WETH 0.05% 池狀態...")
try:
    pool = fetch_pool_via_rpc()
    x_usdc, y_usdc, eth_price = v3_to_v2_equiv(
        pool["sqrtPriceX96"], pool["liquidity"]
    )
    print(f"  sqrtPriceX96 : {pool['sqrtPriceX96']}")
    print(f"  liquidity    : {pool['liquidity']:,}")
    print(f"  ETH 現價     : ${eth_price:,.2f} USDC")
    print(f"  等效 x(USDC) : ${x_usdc:,.0f}")
    print(f"  等效 y(USDC) : ${y_usdc:,.0f}")
    print(f"  等效池深度   : ${x_usdc + y_usdc:,.0f}")
except Exception as e:
    print(f"  RPC 失敗: {e}，使用估算值")
    eth_price = 1900.0
    # 典型 USDC/WETH 0.05% 池：約 $200M TVL，有效深度 ~50M
    x_usdc = 25_000_000
    y_usdc = 25_000_000

pool_depth = x_usdc + y_usdc
print(f"\n使用池深度: ${pool_depth:,.0f} USDC")

# ── 3. 四種規模衝擊計算 ──
sizes = [1_000, 10_000, 100_000, 1_000_000]
size_labels = ["$1k", "$10k", "$100k", "$1M"]
fee = 0.0005  # 0.05%

print("\n=== 四種規模的 Price Impact（v2 精確公式）===")
print(f"{'規模':>8}  {'有效成交價':>14}  {'成交後Spot':>14}  {'衝擊%':>8}  {'手續費':>10}")
print("-" * 65)

results = []
for sz, lbl in zip(sizes, size_labels):
    # USDC 買 WETH：dx = USDC 量，x = USDC 儲備，y = WETH 儲備（以USDC計）
    r = price_impact_v2(x=x_usdc, y=y_usdc, dx=sz, fee=fee)
    results.append(r)
    print(f"{lbl:>8}  {r['effective_price']:>14.8f}  {r['spot_price_after']:>14.8f}  "
          f"{r['price_impact_pct']:>7.4f}%  ${r['fee_cost']:>8.2f}")

# ── 4. 畫圖 ──
fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))
fig.patch.set_facecolor("#0f1117")
for ax in [ax1, ax2]:
    ax.set_facecolor("#1a1d2e")
    ax.tick_params(colors="#c0c0c0")
    ax.xaxis.label.set_color("#c0c0c0")
    ax.yaxis.label.set_color("#c0c0c0")
    ax.title.set_color("#ffffff")
    for spine in ax.spines.values():
        spine.set_edgecolor("#333355")

# 畫連續曲線（log scale x）
import numpy as np
xs = np.logspace(2, 7, 300)  # $100 ~ $10M
impacts = []
eff_prices = []
for dx in xs:
    if dx >= x_usdc * 0.99:  # 避免超過池深度
        impacts.append(None)
        eff_prices.append(None)
        continue
    r = price_impact_v2(x=x_usdc, y=y_usdc, dx=dx, fee=fee)
    impacts.append(r["price_impact_pct"])
    eff_prices.append(r["effective_price"])

valid_x = [x for x, v in zip(xs, impacts) if v is not None]
valid_imp = [v for v in impacts if v is not None]
valid_eff = [v for v in eff_prices if v is not None]

# ax1: 衝擊百分比
ax1.plot(valid_x, valid_imp, color="#4fc3f7", linewidth=2)
ax1.scatter(sizes, [r["price_impact_pct"] for r in results],
            color="#ff6b6b", s=80, zorder=5)
for sz, lbl, r in zip(sizes, size_labels, results):
    ax1.annotate(f"{lbl}\n{r['price_impact_pct']:.3f}%",
                xy=(sz, r["price_impact_pct"]),
                xytext=(10, 10), textcoords="offset points",
                color="#ffcc00", fontsize=8)
ax1.set_xscale("log")
ax1.set_xlabel("交易規模（USDC）")
ax1.set_ylabel("Price Impact (%)")
ax1.set_title(f"Price Impact vs 交易規模\nUniswap v3 USDC/WETH 0.05%  池深≈${pool_depth/1e6:.0f}M")
ax1.xaxis.set_major_formatter(mticker.FuncFormatter(
    lambda x, _: f"${x/1e3:.0f}k" if x < 1e6 else f"${x/1e6:.0f}M"))
ax1.grid(True, alpha=0.2, color="#333355")

# ax2: spot_before vs spot_after（价差展示）
spot_before = results[0]["spot_price_before"]
spot_afters = [r["spot_price_after"] for r in results]
bars = ax2.bar(size_labels,
               [(spot_before - s) / spot_before * 100 for s in spot_afters],
               color=["#4fc3f7", "#26a69a", "#ffca28", "#ef5350"])
ax2.set_xlabel("交易規模")
ax2.set_ylabel("Spot Price 下滑 (%)")
ax2.set_title("成交後 Spot Price 下滑幅度\n（你吃完後，下一個人的起點）")
ax2.grid(True, alpha=0.2, axis="y", color="#333355")
for bar, r in zip(bars, results):
    ax2.text(bar.get_x() + bar.get_width()/2,
             bar.get_height() + 0.001,
             f"{r['price_impact_pct']:.4f}%",
             ha="center", va="bottom", color="#ffffff", fontsize=9)

plt.tight_layout(pad=2.0)
out = "/home/ubuntu/onchain-arb/notes/day02_impact_curve.png"
plt.savefig(out, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
print(f"\n圖已存至 {out}")
