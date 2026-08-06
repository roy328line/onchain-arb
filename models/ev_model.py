"""
ev_model.py — 鏈上套利成本模型 (Day 1 建立，Day 2 完整重構)

架構原則（Day 2 重構後）：
  - 不用「毛利 − 池費 − 衝擊」加減法；改成模擬真實資金流向：
      Q USDC → 池A → W WETH → 池B → Q' USDC，毛利 = Q' − Q
    池費和雙邊衝擊已內嵌在 AMM 公式裡，不再單獨計算。
  - 最優規模 Q 由閉式解求出，不是輸入參數。
  - 失敗成本區分 venue（bundle=0, public/L2=revert×attempts）。
  - p_win sigmoid 加 CALIBRATION WARNING，Day 8 用真實資料校準。
  - 「毛利/池費/衝擊」拆解只用於報表輸出，不參與 EV 計算。

EV 主公式：
  EV = p_win × (Q'−Q − gas − bribe) − (1 − p_win) × f_cost − h_cost
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Literal

import pandas as pd
from scipy.optimize import minimize_scalar


# ──────────────────────────────────────────────
# 1. 資料結構
# ──────────────────────────────────────────────

@dataclass
class PoolState:
    """
    Uniswap v2 池狀態。
    x = 輸入 token 儲備（e.g. USDC）
    y = 輸出 token 儲備（e.g. WETH）
    fee = 池費率（e.g. 0.003 = 0.3%）
    """
    x: float
    y: float
    fee: float = 0.003


@dataclass
class ChainParams:
    """鏈上成本參數，預設為 L1 (Ethereum Mainnet)。"""
    base_gas_usd: float      = 5.0
    priority_fee_usd: float  = 2.0
    revert_gas_usd: float    = 1.5    # bundle 模式不適用
    bridge_fee_usd: float    = 0.0
    n_attempts: int          = 3      # public/l2 venue 才有意義
    venue: Literal["bundle", "public", "l2"] = "public"
    # venue 說明：
    #   "bundle" → Flashbots bundle，失敗不上鏈，f_cost = 0
    #   "public" → 公開 mempool，revert 仍付 gas，f_cost = revert_gas × n_attempts
    #   "l2"     → L2（Arbitrum/Op），revert 率 20-40%，同 public 計算方式


@dataclass
class HoldingParams:
    """資金持有成本參數。Atomic arb 全部為 0。"""
    inventory_usd: float     = 0.0
    hold_time_hours: float   = 0.0
    opportunity_rate: float  = 0.05   # 年化利率
    sigma_daily: float       = 0.03   # 日波動率


@dataclass
class BribeModel:
    """
    p_win ↔ bribe_ratio 的內生模型（sigmoid）。

    p_win(r) = 1 / (1 + exp(−k × (r − midpoint)))

    ⚠️ CALIBRATION WARNING：
      k=5.65 和 midpoint=0.95 均為猜測值。
      目前的「驗證」方式是用兩個自由參數擬合一個假設資料點（bribe=0.8→p_win=0.3），
      再驗算它回到那個點，這是恆等式，不是真正的驗證。
      → Day 8：用真實 atomic arb 歷史資料（Flashbots bundles）做 logistic 回歸校準。
    """
    k: float        = 5.65   # ⚠️ 猜測值，待 Day 8 校準
    midpoint: float = 0.95   # ⚠️ 猜測值，待 Day 8 校準


# ──────────────────────────────────────────────
# 2. AMM 核心（資金流向模擬）
# ──────────────────────────────────────────────

def amm_out(pool: PoolState, dx: float) -> tuple[float, float, float]:
    """
    在 v2 池裡用 dx 單位的 token_x，買入 token_y（含池費）。

    資金流：dx token_x 進池，扣費後用恆定乘積公式解出 dy token_y。
      dx_net = dx × (1 − fee)
      dy = y × dx_net / (x + dx_net)

    Returns: (dy, x_new, y_new)
    """
    if dx <= 0:
        raise ValueError("dx 必須 > 0")
    dx_net = dx * (1 - pool.fee)
    dy = pool.y * dx_net / (pool.x + dx_net)
    return dy, pool.x + dx_net, pool.y - dy


def simulate_arb(pool_a: PoolState, pool_b: PoolState, Q: float) -> dict:
    """
    模擬完整的雙池套利資金流向：
      Q token0 → 池A（買 token1） → W token1 → 池B（買 token0） → Q' token0

    池A: x=token0, y=token1（輸入 token0，得到 token1）
    池B: x=token1, y=token0（輸入 token1，得到 token0）

    池費和雙邊衝擊均已內嵌，不再拆解為獨立項。
    net = Q' − Q（已含所有摩擦，< 0 代表虧損）
    """
    W, _, _ = amm_out(pool_a, dx=Q)          # Q token0 → W token1（池A）
    Q_out, _, _ = amm_out(pool_b, dx=W)      # W token1 → Q' token0（池B）
    net = Q_out - Q
    return {
        "Q_in":       Q,
        "W":          round(W, 8),
        "Q_out":      round(Q_out, 6),
        "net":        round(net, 6),
        "profit_pct": round(net / Q * 100, 6),
    }


# ──────────────────────────────────────────────
# 3. 最優規模（閉式解 + 數值驗證）
# ──────────────────────────────────────────────

def optimal_size(pool_a: PoolState, pool_b: PoolState) -> dict:
    """
    計算雙池套利的最優輸入規模 Q*（閉式解）。

    符號定義（對應 simulate_arb）：
      Ra0 = pool_a.x  （token0 在池A的儲備，e.g. USDC）
      Ra1 = pool_a.y  （token1 在池A的儲備，e.g. WETH）
      Rb0 = pool_b.x  （token1 在池B的儲備，e.g. WETH）
      Rb1 = pool_b.y  （token0 在池B的儲備，e.g. USDC）
      γ1 = 1 − pool_a.fee，γ2 = 1 − pool_b.fee

    閉式解（推導自 d(Q'−Q)/dQ = 1，即邊際報酬等於邊際成本）：
      numer = √(γ1·γ2·Ra0·Ra1·Rb0·Rb1) − Ra0·Rb0
      denom = γ2·Rb0 + γ1·γ2·Ra1

    numer < 0 → 此方向無利可圖。

    Roy 原公式 notation 中的 Ra1,Ra2,Rb1,Rb2 對應：
      Ra1→Ra0, Ra2→Ra1, Rb1→Rb0, Rb2→Rb1
      分母 (γ1·Rb2 + γ1·γ2·Rb1) = (γ1·Rb1 + γ1·γ2·Rb0)
      當 Rb0=Ra1（中間 token 兩池深度相等）時與正確公式等價，
      但 pool_a.fee ≠ pool_b.fee 時要小心 γ1 vs γ2 的位置。

    同時用 scipy 數值最佳化驗證，誤差需 < 0.1%。
    """
    g1 = 1 - pool_a.fee
    g2 = 1 - pool_b.fee
    Ra0 = pool_a.x
    Ra1 = pool_a.y
    Rb0 = pool_b.x
    Rb1 = pool_b.y

    inner = g1 * g2 * Ra0 * Ra1 * Rb0 * Rb1
    numer = math.sqrt(inner) - Ra0 * Rb0
    denom = g2 * Rb0 + g1 * g2 * Ra1

    if numer <= 0:
        return {
            "Q_star": 0.0, "net_star": 0.0,
            "direction": "no_opportunity",
            "numeric_Q": 0.0, "error_pct": 0.0,
        }

    Q_star = numer / denom

    # 數值驗證（scipy minimize_scalar）
    def neg_profit(Q):
        if Q <= 0:
            return 0.0
        try:
            return -simulate_arb(pool_a, pool_b, Q)["net"]
        except Exception:
            return 0.0

    bound = min(Ra0, Rb1) * 0.5
    res = minimize_scalar(neg_profit, bounds=(1.0, bound), method="bounded")
    numeric_Q = res.x
    net_star  = simulate_arb(pool_a, pool_b, Q_star)["net"]
    error_pct = abs(Q_star - numeric_Q) / max(numeric_Q, 1e-9) * 100

    return {
        "Q_star":    round(Q_star, 4),
        "net_star":  round(net_star, 6),
        "direction": "A→B",
        "numeric_Q": round(numeric_Q, 4),
        "error_pct": round(error_pct, 4),
    }


# ──────────────────────────────────────────────
# 4. 成本函式
# ──────────────────────────────────────────────

def failure_cost(chain: ChainParams) -> float:
    """
    失敗成本（venue 決定）。

    "bundle"  → Flashbots bundle 失敗不上鏈 → f_cost = 0
    "public"  → 公開 mempool revert 仍扣 gas → f_cost = revert_gas × n_attempts
    "l2"      → L2 revert（率 20-40%），同 public 計算方式
    """
    if chain.venue == "bundle":
        return 0.0
    return chain.revert_gas_usd * chain.n_attempts


def holding_cost(holding: HoldingParams) -> float:
    """
    資金持有成本 = 機會成本 + 價格風險調整項

    opportunity = inventory × t_years × rate

    price_risk = inventory × σ_daily × √t_days
      ⚠️ 這是「風險調整項」，不是期望成本。
         隨機遊走的期望價格變動 = 0，price_risk 捕捉的是
         持有期間 1-sigma 的 mark-to-market 資金敞口風險。

    t_years 與 t_days 均由同一個 hold_time_hours 推導，確保一致：
      t_years = hold_time_hours / (365 × 24)
      t_days  = hold_time_hours / 24  （= t_years × 365）
    """
    if holding.inventory_usd <= 0 or holding.hold_time_hours <= 0:
        return 0.0

    t_years = holding.hold_time_hours / (365 * 24)
    t_days  = holding.hold_time_hours / 24   # 同源推導，不重複定義常數

    opportunity = holding.inventory_usd * t_years * holding.opportunity_rate
    price_risk  = holding.inventory_usd * holding.sigma_daily * math.sqrt(t_days)

    return opportunity + price_risk


def p_win_from_bribe(bribe_ratio: float, model: BribeModel) -> float:
    """
    內生 p_win：由 bribe_ratio 透過 sigmoid 決定。
    ⚠️ 見 BribeModel 的 CALIBRATION WARNING。
    """
    x = model.k * (bribe_ratio - model.midpoint)
    return 1.0 / (1.0 + math.exp(-x))


# ──────────────────────────────────────────────
# 5. 主 EV 函式
# ──────────────────────────────────────────────

def compute_ev(
    bribe_ratio: float,
    pool_a: PoolState,
    pool_b: PoolState,
    Q: float,
    chain: ChainParams,
    holding: Optional[HoldingParams] = None,
    bribe_model: Optional[BribeModel] = None,
) -> dict:
    """
    計算單一 bribe_ratio 下的期望值。

    net_raw = Q' − Q（已含雙邊池費與衝擊，直接從 simulate_arb 取得）
    EV = p_win × (net_raw − gas − bribe) − (1−p_win) × f_cost − h_cost

    「毛利/池費/衝擊」的拆解只用於報表輸出，不參與此計算。
    """
    if holding is None:
        holding = HoldingParams()
    if bribe_model is None:
        bribe_model = BribeModel()

    arb       = simulate_arb(pool_a, pool_b, Q)
    net_raw   = arb["net"]

    gas_cost  = chain.base_gas_usd + chain.priority_fee_usd + chain.bridge_fee_usd
    bribe_usd = bribe_ratio * net_raw if net_raw > 0 else 0.0
    f_cost    = failure_cost(chain)
    h_cost    = holding_cost(holding)
    pw        = p_win_from_bribe(bribe_ratio, bribe_model)

    net_after = net_raw - gas_cost - bribe_usd
    ev = pw * net_after - (1 - pw) * f_cost - h_cost

    return {
        "bribe_ratio": bribe_ratio,
        "p_win":       round(pw, 4),
        "net_raw":     round(net_raw, 4),
        "gas_cost":    round(gas_cost, 4),
        "bribe_usd":   round(bribe_usd, 4),
        "f_cost":      round(f_cost, 4),
        "h_cost":      round(h_cost, 4),
        "net_after":   round(net_after, 4),
        "ev":          round(ev, 4),
    }


# ──────────────────────────────────────────────
# 6. EV 曲線 + 敏感度
# ──────────────────────────────────────────────

def sweep_bribe(
    bribe_ratios: list[float],
    pool_a: PoolState,
    pool_b: PoolState,
    Q: float,
    chain: ChainParams,
    holding: Optional[HoldingParams] = None,
    bribe_model: Optional[BribeModel] = None,
) -> pd.DataFrame:
    """掃描不同 bribe_ratio，回傳 EV 曲線 DataFrame。"""
    rows = [
        compute_ev(r, pool_a, pool_b, Q, chain, holding, bribe_model)
        for r in bribe_ratios
    ]
    return pd.DataFrame(rows)


def bribe_sensitivity(
    bribe_ratios: list[float],
    k_values: list[float],
    pool_a: PoolState,
    pool_b: PoolState,
    Q: float,
    chain: ChainParams,
    holding: Optional[HoldingParams] = None,
) -> pd.DataFrame:
    """
    EV 對 bribe_ratio × k 的敏感度分析。
    ⚠️ 因 k 是猜測值，此分析揭示模型對 k 的依賴程度，Day 8 校準前不可輕信 EV 絕對值。
    """
    rows = []
    for k in k_values:
        model = BribeModel(k=k, midpoint=0.95)
        for r in bribe_ratios:
            result = compute_ev(r, pool_a, pool_b, Q, chain, holding, model)
            rows.append({"bribe_ratio": r, "k": k, "ev": result["ev"]})
    return pd.DataFrame(rows)


# ──────────────────────────────────────────────
# 7. 測試
# ──────────────────────────────────────────────

def test_double_sided_impact():
    """
    驗證雙邊衝擊遠超過單邊估計的誤差。

    場景：約 1% 價差、0.3% 費
      pool_a：6,000,000 USDC / 3,000 WETH  (spot $2,000/WETH)
      pool_b：3,000 WETH / 6,060,000 USDC  (spot $2,020/WETH，約 1% 高)
      Q = 20,000 USDC

    單邊近似：假設池B完全無衝擊，以 spot price 換算。
    雙邊真實：simulate_arb 含兩池衝擊。
    """
    pool_a = PoolState(x=6_000_000, y=3_000, fee=0.003)
    pool_b = PoolState(x=3_000, y=6_060_000, fee=0.003)
    Q = 20_000

    # 雙邊真實
    result = simulate_arb(pool_a, pool_b, Q)

    # 單邊近似（只算池A，池B用 spot price 無衝擊計算）
    W, _, _ = amm_out(pool_a, dx=Q)
    spot_b = pool_b.y / pool_b.x
    Q_out_single = W * spot_b * (1 - pool_b.fee)
    single_net   = Q_out_single - Q

    spot_a = pool_a.x / pool_a.y
    spot_b_price = pool_b.y / pool_b.x

    print("=" * 60)
    print("  test_double_sided_impact")
    print("=" * 60)
    print(f"  Pool A：{pool_a.x/1e6:.1f}M USDC / {pool_a.y:.0f} WETH  (spot ${spot_a:,.0f}/WETH)")
    print(f"  Pool B：{pool_b.x:.0f} WETH / {pool_b.y/1e6:.2f}M USDC  (spot ${spot_b_price:,.0f}/WETH)")
    print(f"  Q = ${Q:,} USDC → W = {result['W']:.4f} WETH")
    print()
    print(f"  單邊近似 net : ${single_net:>+10.2f}  ← 只算池A，池B假設無衝擊")
    print(f"  雙邊真實 net : ${result['net']:>+10.2f}  ← 含兩池衝擊")
    print(f"  誤差         : ${single_net - result['net']:>+10.2f}")
    print()

    assert result["net"] < single_net, "雙邊淨利應小於單邊近似"
    if result["net"] < 0 < single_net:
        print("  ✅ 單邊顯示獲利，雙邊真實是虧損——關鍵誤差，不容忽視")
    else:
        print(f"  ✅ 雙邊 ({result['net']:.2f}) < 單邊 ({single_net:.2f})")
    print("=" * 60)
    return result


def test_optimal_size():
    """驗證閉式解最優規模，誤差 < 0.1%。"""
    pool_a = PoolState(x=6_000_000, y=3_000, fee=0.003)
    pool_b = PoolState(x=3_000, y=6_060_000, fee=0.003)

    res = optimal_size(pool_a, pool_b)
    print()
    print("=" * 60)
    print("  test_optimal_size")
    print("=" * 60)
    if res["direction"] == "no_opportunity":
        print("  此方向無套利機會")
    else:
        print(f"  閉式解 Q*  : ${res['Q_star']:>10,.2f}")
        print(f"  數值解 Q*  : ${res['numeric_Q']:>10,.2f}")
        print(f"  誤差       : {res['error_pct']:.4f}%")
        print(f"  Q* 下毛利  : ${res['net_star']:>10,.4f}")
        assert res["error_pct"] < 0.1, f"誤差 {res['error_pct']:.4f}% 超過 0.1%"
        print("  ✅ 閉式解誤差 < 0.1%")
    print("=" * 60)
    return res


def test_venue_failure_cost():
    """驗證 venue 決定失敗成本。"""
    c_bundle = ChainParams(venue="bundle", revert_gas_usd=1.5, n_attempts=3)
    c_public = ChainParams(venue="public", revert_gas_usd=1.5, n_attempts=3)
    c_l2     = ChainParams(venue="l2",     revert_gas_usd=0.01, n_attempts=3)

    assert failure_cost(c_bundle) == 0.0,  "bundle f_cost 應為 0"
    assert failure_cost(c_public) == 4.5,  "public f_cost 應為 4.5"
    assert abs(failure_cost(c_l2) - 0.03) < 1e-9, "l2 f_cost 應為 0.03"
    print()
    print("  test_venue_failure_cost ✅")
    print(f"    bundle : ${failure_cost(c_bundle)}")
    print(f"    public : ${failure_cost(c_public)}")
    print(f"    l2     : ${failure_cost(c_l2)}")


# ──────────────────────────────────────────────
# 8. 驗收區塊
# ──────────────────────────────────────────────

if __name__ == "__main__":
    import numpy as np

    double_result = test_double_sided_impact()
    opt_result    = test_optimal_size()
    test_venue_failure_cost()

    # EV 曲線（用最優規模）
    pool_a = PoolState(x=6_000_000, y=3_000, fee=0.003)
    pool_b = PoolState(x=3_000, y=6_060_000, fee=0.003)
    chain  = ChainParams(venue="public")

    Q_star = opt_result["Q_star"] if opt_result["Q_star"] > 0 else 5_000.0
    print()
    print("=" * 60)
    print(f"  EV 曲線（Q*={Q_star:,.0f} USDC，venue=public）")
    print("  ⚠️ p_win 基於猜測 sigmoid，絕對值不可輕信")
    print("=" * 60)
    ratios = np.arange(0.0, 1.05, 0.1).tolist()
    df = sweep_bribe(ratios, pool_a, pool_b, Q_star, chain)
    print(df[["bribe_ratio", "p_win", "net_raw", "net_after", "ev"]].to_string(index=False))
    best = df.loc[df["ev"].idxmax()]
    print(f"\n  最高 EV：bribe={best['bribe_ratio']:.1f}，EV=${best['ev']:.4f}")

    # k 敏感度
    print()
    print("=" * 60)
    print("  k 敏感度（⚠️ k 是猜測值，EV 結論對 k 高度敏感）")
    print("=" * 60)
    df_s = bribe_sensitivity(
        [0.3, 0.5, 0.7], [2.0, 5.65, 10.0, 20.0],
        pool_a, pool_b, Q_star, chain
    )
    print(df_s.pivot(index="bribe_ratio", columns="k", values="ev").to_string())
    print("\n  → Day 8 前，bribe 掃描結果僅供定性參考，不可作為決策依據")
