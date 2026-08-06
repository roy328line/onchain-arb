"""
ev_model.py — 鏈上套利成本模型 (Day 1)

EV = p_win × (毛利 − 成功成本) − (1 − p_win) × 失敗成本 − 資金持有成本

三個設計重點：
  1. p_win 與 bribe_ratio 是內生變數（sigmoid 綁定），不可獨立傳入
  2. 價格衝擊非線性，買賣兩端各吃
  3. 失敗成本不是小修正項（L2 revert 率 20-40%）
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd


# ──────────────────────────────────────────────
# 1. 資料結構
# ──────────────────────────────────────────────

@dataclass
class TradeParams:
    """單筆交易的市場參數。"""
    Q: float              # 套利數量（USD 計價）
    delta_p: float        # 兩池價差（USD / unit，已扣除基礎 token 換算）
    pool_depth: float     # 池深度（USD），用於計算價格衝擊
    n_attempts: int = 3   # 失敗重試次數（影響失敗成本）


@dataclass
class ChainParams:
    """鏈上成本參數，預設為 L1 (Ethereum Mainnet) 合理估計值。"""
    pool_fee_rate: float  = 0.003      # 池費率（e.g. 0.3% = 0.003）
    slippage_gap: float   = 0.0        # 滑點缺口（USD）；可從訂單簿估
    base_gas_usd: float   = 5.0        # BaseGas 成本（USD）
    priority_fee_usd: float = 2.0      # PriorityFee / tip（USD）
    revert_gas_usd: float  = 1.5       # 單次 revert gas（USD）
    bridge_fee_usd: float  = 0.0       # 橋費（L1 atomic arb = 0）
    eth_price_usd: float   = 3000.0    # ETH 現價（用於 gas 換算參考）

    # L2 params（若切換鏈時覆蓋）
    # base_gas_usd: 0.05, revert_gas_usd: 0.01, ...


@dataclass
class HoldingParams:
    """資金持有成本參數。"""
    inventory_usd: float      = 0.0     # 需鎖定的庫存（USD）
    hold_time_hours: float    = 0.0     # 持有時間（小時）；atomic arb ≈ 0
    opportunity_rate: float   = 0.05    # 機會成本年化利率（5%）
    sigma_daily: float        = 0.03    # 標的日波動率（3%）


@dataclass
class BribeModel:
    """
    p_win ↔ bribe_ratio 的內生模型（sigmoid）。

    p_win(r) = 1 / (1 + exp(−k × (r − midpoint)))

    校準方式：讓 bribe_ratio=0.8 → p_win≈0.3
    反推：
      1/(1+exp(-k*(0.8-mid))) = 0.3
      k*(mid - 0.8) = ln(7/3) ≈ 0.8473
    取 midpoint=0.95 → k = 0.8473 / (0.95-0.8) ≈ 5.65
    """
    k: float        = 5.65   # 陡峭程度（校準自 bribe=0.8→p_win≈0.3）
    midpoint: float = 0.95   # 50% 勝率對應的 bribe_ratio


# ──────────────────────────────────────────────
# 2. 成本子函式
# ──────────────────────────────────────────────

def gross_profit(Q: float, delta_p: float) -> float:
    """毛利 = Q × ΔP"""
    return Q * delta_p


def price_impact(Q: float, pool_depth: float) -> float:
    """
    非線性價格衝擊：買賣兩端各吃 Q²/(2×depth)

    假設恆定乘積 AMM (xy=k)：
      impact per side = Q / (2 × depth)  （線性近似，保守）
      total impact ≈ Q² / depth           （兩側相加，Q² 項）

    這裡採保守估計（兩側都交易同一深度的池）：
      total_impact_cost = Q × (Q / pool_depth)  = Q² / pool_depth
    """
    if pool_depth <= 0:
        return float("inf")
    return (Q ** 2) / pool_depth


def price_impact_v2(
    x: float,
    y: float,
    dx: float,
    fee: float,
) -> dict:
    """
    精確的 Uniswap v2 價格衝擊計算。

    x, y  : 池中兩種 token 的儲備量（同單位，e.g. USD）
    dx    : 買入量（x token，扣費前）
    fee   : 池費率（e.g. 0.003 = 0.3%）

    兩件事分開算：
      1. effective_price  : 有效成交價 = 實際付出 dx，得到多少 dy
                            dy = y * dx_net / (x + dx_net)
                            effective_price = dy / dx   ← 含滑點的均價
      2. spot_price_after : 成交後的邊際價格 = y' / x'
                            x' = x + dx_net
                            y' = k / x'  =  y - dy
                            spot_price_after = y' / x'

    兩者不同：effective_price 是整筆均價，spot_price_after 是下一單的起點。

    Returns dict:
        dy              實際得到的 y token 量
        effective_price 有效成交均價（dy/dx，含滑點）
        spot_price_before 成交前 spot price（y/x）
        spot_price_after  成交後 spot price（y'/x'）
        price_impact_pct  衝擊百分比（(before-after)/before）
        fee_cost        實際扣掉的手續費（dx 單位）
    """
    if x <= 0 or y <= 0 or dx <= 0:
        raise ValueError("x, y, dx 必須 > 0")

    spot_before = y / x            # 成交前 spot price
    fee_cost    = dx * fee         # 手續費（dx token）
    dx_net      = dx * (1 - fee)   # 扣費後實際進池的量

    # Uniswap v2 恆定乘積：x·y = k = (x + dx_net)(y - dy)
    # → dy = y * dx_net / (x + dx_net)
    dy = y * dx_net / (x + dx_net)

    x_after     = x + dx_net
    y_after     = y - dy
    spot_after  = y_after / x_after

    effective_price  = dy / dx     # 含費、含滑點的均價
    impact_pct       = (spot_before - spot_after) / spot_before * 100

    return {
        "dy":                round(dy, 6),
        "effective_price":   round(effective_price, 8),
        "spot_price_before": round(spot_before, 8),
        "spot_price_after":  round(spot_after, 8),
        "price_impact_pct":  round(impact_pct, 6),
        "fee_cost":          round(fee_cost, 6),
    }


def p_win_from_bribe(bribe_ratio: float, model: BribeModel) -> float:
    """
    內生 p_win：由 bribe_ratio 透過 sigmoid 決定。

    bribe_ratio ∈ [0, 1]，代表 bribe 佔毛利的比例。
    """
    x = model.k * (bribe_ratio - model.midpoint)
    return 1.0 / (1.0 + math.exp(-x))


def success_cost(
    Q: float,
    gross: float,
    bribe_ratio: float,
    trade: TradeParams,
    chain: ChainParams,
) -> float:
    """
    成功端成本 = 池費 + 價格衝擊 + 滑點缺口 + BaseGas + PriorityFee + Bribe + 橋費

    注意：Bribe 以毛利的比例計算（內生）。
    """
    pool_fee    = Q * chain.pool_fee_rate
    impact      = price_impact(Q, trade.pool_depth)
    slippage    = chain.slippage_gap
    gas         = chain.base_gas_usd + chain.priority_fee_usd
    bribe       = bribe_ratio * gross
    bridge      = chain.bridge_fee_usd

    return pool_fee + impact + slippage + gas + bribe + bridge


def failure_cost(trade: TradeParams, chain: ChainParams) -> float:
    """
    失敗成本 = revert_gas × n_attempts

    L2 上 revert 率 20-40%，這不是小修正項。
    """
    return chain.revert_gas_usd * trade.n_attempts


def holding_cost(holding: HoldingParams) -> float:
    """
    資金持有成本 = 庫存 × 時間 × 機會成本 + 庫存價格風險(σ×√t)

    時間單位：小時 → 年化
    σ 是日波動率，√t 以日為單位
    """
    if holding.inventory_usd <= 0 or holding.hold_time_hours <= 0:
        return 0.0

    t_years = holding.hold_time_hours / (365 * 24)
    t_days  = holding.hold_time_hours / 24

    opportunity = holding.inventory_usd * t_years * holding.opportunity_rate
    price_risk  = holding.inventory_usd * holding.sigma_daily * math.sqrt(t_days)

    return opportunity + price_risk


# ──────────────────────────────────────────────
# 3. 主 EV 函式
# ──────────────────────────────────────────────

def compute_ev(
    bribe_ratio: float,
    trade: TradeParams,
    chain: ChainParams,
    holding: Optional[HoldingParams] = None,
    bribe_model: Optional[BribeModel] = None,
) -> dict:
    """
    計算單一 bribe_ratio 下的期望值。

    Returns dict:
        ev, gross, bribe_usd, p_win, s_cost, f_cost, h_cost, net_win
    """
    if holding is None:
        holding = HoldingParams()
    if bribe_model is None:
        bribe_model = BribeModel()

    gross   = gross_profit(trade.Q, trade.delta_p)
    pw      = p_win_from_bribe(bribe_ratio, bribe_model)
    s_cost  = success_cost(trade.Q, gross, bribe_ratio, trade, chain)
    f_cost  = failure_cost(trade, chain)
    h_cost  = holding_cost(holding)

    net_win = gross - s_cost          # 成功時的淨利
    ev = pw * net_win - (1 - pw) * f_cost - h_cost

    return {
        "bribe_ratio": bribe_ratio,
        "p_win":       round(pw, 4),
        "gross":       round(gross, 4),
        "bribe_usd":   round(bribe_ratio * gross, 4),
        "s_cost":      round(s_cost, 4),
        "f_cost":      round(f_cost, 4),
        "h_cost":      round(h_cost, 4),
        "net_win":     round(net_win, 4),
        "ev":          round(ev, 4),
    }


# ──────────────────────────────────────────────
# 4. Bribe 掃描器
# ──────────────────────────────────────────────

def sweep_bribe(
    bribe_ratios: list[float],
    trade: TradeParams,
    chain: ChainParams,
    holding: Optional[HoldingParams] = None,
    bribe_model: Optional[BribeModel] = None,
) -> pd.DataFrame:
    """
    掃描不同 bribe_ratio，輸出 EV 曲線 DataFrame。

    Columns: bribe_ratio, p_win, gross, bribe_usd, s_cost, f_cost, h_cost, net_win, ev
    """
    rows = [
        compute_ev(r, trade, chain, holding, bribe_model)
        for r in bribe_ratios
    ]
    return pd.DataFrame(rows)


# ──────────────────────────────────────────────
# 5. 驗收區塊
# ──────────────────────────────────────────────

if __name__ == "__main__":
    import numpy as np

    print("=" * 55)
    print("  驗收 Case: L1 Atomic Arb（毛利 $200，bribe=80%）")
    print("=" * 55)

    # 場景：毛利 $200 的 L1 atomic arb
    #   Q × ΔP = 200  →  Q=10000 USD，ΔP=0.02 USD
    #   pool_depth=5,000,000 USD（中型 Uniswap v3 池，$10M TVL → 有效深度約一半）
    trade = TradeParams(
        Q           = 10_000,
        delta_p     = 0.02,
        pool_depth  = 5_000_000,
        n_attempts  = 3,
    )
    chain = ChainParams()       # L1 預設
    holding = HoldingParams()   # atomic arb → 無持有成本

    bribe_ratio = 0.80
    result = compute_ev(bribe_ratio, trade, chain, holding)

    print(f"\n  gross_profit   : ${result['gross']:>10.2f}")
    print(f"  bribe_ratio    : {result['bribe_ratio']:.2f}  →  bribe = ${result['bribe_usd']:.2f}")
    print(f"  p_win          : {result['p_win']:.4f}")
    print(f"  success_cost   : ${result['s_cost']:>10.2f}")
    print(f"    └─ 細項：")
    gross   = result["gross"]
    Q, pool_depth = trade.Q, trade.pool_depth
    print(f"       池費       : ${Q * chain.pool_fee_rate:>8.2f}")
    print(f"       價格衝擊   : ${price_impact(Q, pool_depth):>8.2f}")
    print(f"       滑點缺口   : ${chain.slippage_gap:>8.2f}")
    print(f"       BaseGas    : ${chain.base_gas_usd:>8.2f}")
    print(f"       PriorityFee: ${chain.priority_fee_usd:>8.2f}")
    print(f"       Bribe      : ${bribe_ratio * gross:>8.2f}")
    print(f"       橋費       : ${chain.bridge_fee_usd:>8.2f}")
    print(f"  failure_cost   : ${result['f_cost']:>10.2f}")
    print(f"  holding_cost   : ${result['h_cost']:>10.2f}")
    print(f"  net_win (成功) : ${result['net_win']:>10.2f}")
    print(f"\n  ─────────────────────────────────────────")
    ev = result["ev"]
    sign = "✅ 負的" if ev < 0 else "❌ 應為負"
    print(f"  EV             : ${ev:>10.2f}  ← {sign}")
    print("=" * 55)

    # ── Bribe 掃描 ──
    print("\n  Bribe 掃描（EV 曲線）")
    print("  " + "-" * 53)
    ratios = np.arange(0.0, 1.05, 0.1).tolist()
    df = sweep_bribe(ratios, trade, chain, holding)
    print(df[["bribe_ratio", "p_win", "net_win", "ev"]].to_string(index=False))
    print()
    best = df.loc[df["ev"].idxmax()]
    print(f"  最高 EV 出現在 bribe_ratio={best['bribe_ratio']:.1f}，EV=${best['ev']:.2f}")
    print("=" * 55)
