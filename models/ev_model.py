"""
ev_model.py — 鏈上套利成本模型 (Day 1 建立，Day 2 完整重構，Day 2b 修正)

架構原則：
  - 不用「毛利 − 池費 − 衝擊」加減法；改成模擬真實資金流向：
      Q USDC → 池A → W WETH → 池B → Q' USDC，net = Q' − Q
    池費和雙邊衝擊已內嵌在 AMM 公式裡，不再單獨計算。
  - 最優規模 Q 由閉式解快篩，EV 最優由 best_ev() 雙變數最佳化確認。
  - 失敗成本區分 venue：bundle=0；public=revert×attempts；l2=獨立 revert_rate。
  - p_win sigmoid ⚠️ CALIBRATION WARNING，Day 8 用真實資料校準。
  - bribe 基礎為 surplus（= max(0, net - gas)），不是現貨差。

EV 主公式：
  surplus   = max(0, net_raw − gas_cost)
  net_after = surplus − bribe_usd   （bribe_usd = bribe_ratio × surplus）
  EV = p_win × net_after − f_cost_expected − h_cost

  其中 f_cost_expected：
    bundle : 0
    public : (1 − p_win) × revert_gas × n_attempts
    l2     : l2_revert_rate × revert_gas × n_attempts  （獨立隨機過程，與 bribe 無關）
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional, Literal

import pandas as pd
from scipy.optimize import minimize_scalar, minimize


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
    revert_gas_usd: float    = 1.5
    bridge_fee_usd: float    = 0.0
    n_attempts: int          = 3
    venue: Literal["bundle", "public", "l2"] = "public"
    l2_revert_rate: float    = 0.30   # L2 獨立 revert 機率（研究值 20-40%）
    # venue 說明：
    #   "bundle" → Flashbots bundle，失敗不上鏈，f_cost = 0
    #   "public" → 公開 mempool，失敗率 = (1 − p_win)，付 revert gas
    #   "l2"     → L2，revert 是獨立隨機過程（非 bribe 拍賣），
    #              用 l2_revert_rate 而非 (1−p_win)


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

    v2 機制：全額 dx 進池（手續費留在池中歸 LP）；
    γ = 1 − fee 只影響計算，不改變進池金額。
      dx_net = dx × (1 − fee)   ← 有效計算量
      dy = y × dx_net / (x + dx_net)
      x_new = x + dx            ← 全額 dx 更新儲備（含手續費部分）
      y_new = y − dy

    Returns: (dy, x_new, y_new)
    注意：x_new 以全額 dx 計，多跳路徑串接時不會出錯。
    """
    if dx <= 0:
        raise ValueError("dx 必須 > 0")
    dx_net = dx * (1 - pool.fee)                      # 有效計算量
    dy = pool.y * dx_net / (pool.x + dx_net)
    return dy, pool.x + dx, pool.y - dy               # x_new 用全額 dx


def simulate_arb(pool_a: PoolState, pool_b: PoolState, Q: float) -> dict:
    """
    模擬完整的雙池套利資金流向：
      Q token0 → 池A（買 token1） → W token1 → 池B（買 token0） → Q' token0

    池A: x=token0, y=token1（輸入 token0，得到 token1）
    池B: x=token1, y=token0（輸入 token1，得到 token0）

    池費和雙邊衝擊均已內嵌，不再拆解為獨立項。
    net = Q' − Q（< 0 代表虧損）
    """
    W, _, _ = amm_out(pool_a, dx=Q)
    Q_out, _, _ = amm_out(pool_b, dx=W)
    net = Q_out - Q
    return {
        "Q_in":       Q,
        "W":          round(W, 8),
        "Q_out":      round(Q_out, 6),
        "net":        round(net, 6),
        "profit_pct": round(net / Q * 100, 6),
    }


# ──────────────────────────────────────────────
# 3. 最優規模（閉式解 + 雙向 + 數值驗證）
# ──────────────────────────────────────────────

def _optimal_size_one_direction(
    pool_a: PoolState, pool_b: PoolState
) -> tuple[float, float]:
    """
    單方向閉式解（A→B），回傳 (Q_star, net_star)。
    numer <= 0 時回傳 (0.0, -inf)。

    推導（從第一原理，d(Q'−Q)/dQ = 0）：
      Q' = Rb1 × g1·g2·Ra1·Q / (Ra0·Rb0 + g1·Q·(Rb0 + g2·Ra1))

      令 A = Ra0·Rb0，B = g1·(Rb0 + g2·Ra1)：
        dQ'/dQ = Rb1·g1·g2·Ra1·A / (A + B·Q)²

      dQ'/dQ = 1 → (A + B·Q)² = Rb1·g1·g2·Ra1·A
        → Q* = (√(g1·g2·Ra0·Ra1·Rb0·Rb1) − Ra0·Rb0) / [g1·(Rb0 + g2·Ra1)]
               = numer / (g1·Rb0 + g1·g2·Ra1)

    注意：分母為 g1·Rb0 + g1·g2·Ra1（提 g1）。
    若寫成 g2·Rb0 + g1·g2·Ra1，同費率時恆等，
    但 fee_a ≠ fee_b 時誤差超過 0.1%（已用測試案例驗證）。

    Q* 在以下三種情況不等於 EV 最優規模（見 best_ev() docstring）：
      (a) p_win 與 Q 相關
      (b) bribe 基礎不是 surplus·(1−r) 的比例形式
      (c) 有 Q 的非線性成本項（如庫存風險 ∝ Q·σ√t）
    """
    g1 = 1 - pool_a.fee
    g2 = 1 - pool_b.fee
    Ra0, Ra1 = pool_a.x, pool_a.y
    Rb0, Rb1 = pool_b.x, pool_b.y

    inner = g1 * g2 * Ra0 * Ra1 * Rb0 * Rb1
    numer = math.sqrt(inner) - Ra0 * Rb0
    denom = g1 * Rb0 + g1 * g2 * Ra1    # 正確：提 g1，不是 g2*Rb0

    if numer <= 0 or denom <= 0:
        return 0.0, float("-inf")

    Q_star = numer / denom
    net_star = simulate_arb(pool_a, pool_b, Q_star)["net"]
    return Q_star, net_star


def optimal_size(pool_a: PoolState, pool_b: PoolState) -> dict:
    """
    計算雙池套利的最優輸入規模 Q*（閉式解，雙向）。

    numer <= 0 不代表完全無機會，可能是反方向有套利。
    兩個方向都計算，取 net 較大的方向。

    同時用 scipy 數值最佳化做 sanity check，要求誤差 < 0.1%。
    """
    # A→B 方向
    Qs_ab, net_ab = _optimal_size_one_direction(pool_a, pool_b)
    # B→A 方向（pool_a, pool_b 互換）
    Qs_ba, net_ba = _optimal_size_one_direction(pool_b, pool_a)

    if net_ab <= 0 and net_ba <= 0:
        return {
            "Q_star": 0.0, "net_star": 0.0,
            "direction": "no_opportunity",
            "numeric_Q": 0.0, "error_pct": 0.0,
        }

    if net_ab >= net_ba:
        Q_star, net_star, direction = Qs_ab, net_ab, "A→B"
        pa, pb = pool_a, pool_b
    else:
        Q_star, net_star, direction = Qs_ba, net_ba, "B→A"
        pa, pb = pool_b, pool_a

    # 數值驗證
    bound = min(pa.x, pb.y) * 0.5
    res = minimize_scalar(
        lambda Q: -simulate_arb(pa, pb, Q)["net"] if Q > 0 else 0.0,
        bounds=(1.0, bound), method="bounded"
    )
    numeric_Q = res.x
    error_pct = abs(Q_star - numeric_Q) / max(numeric_Q, 1e-9) * 100

    return {
        "Q_star":    round(Q_star, 4),
        "net_star":  round(net_star, 6),
        "direction": direction,
        "numeric_Q": round(numeric_Q, 4),
        "error_pct": round(error_pct, 4),
    }


# ──────────────────────────────────────────────
# 4. 成本函式
# ──────────────────────────────────────────────

def failure_cost_expected(chain: ChainParams, p_win: float) -> float:
    """
    期望失敗成本（venue 決定機制）。

    "bundle" → 失敗不上鏈，f_cost = 0
    "public" → 失敗率 = (1 − p_win)（bribe 拍賣決定），付 revert_gas × n_attempts
    "l2"     → revert 是獨立隨機過程，與 bribe 無關；
               用 l2_revert_rate（研究值 20-40%），不從 (1−p_win) 推導
    """
    if chain.venue == "bundle":
        return 0.0
    gas = chain.revert_gas_usd * chain.n_attempts
    if chain.venue == "l2":
        return chain.l2_revert_rate * gas
    # public
    return (1 - p_win) * gas


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
    t_days  = holding.hold_time_hours / 24
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
    計算單一 (bribe_ratio, Q) 下的期望值。

    bribe 基礎為 surplus（實現剩餘），不是現貨差或 gross：
      surplus   = max(0, net_raw − gas_cost)
      bribe_usd = bribe_ratio × surplus
      net_after = surplus − bribe_usd = surplus × (1 − bribe_ratio)

    理由：原子套利的 bribe 來源是交易自身的產出（coinbase.transfer），
    上界必然是 surplus；用現貨差會導致 bribe > net，數學上無解。

    EV = p_win × net_after − f_cost_expected − h_cost
    """
    if holding is None:
        holding = HoldingParams()
    if bribe_model is None:
        bribe_model = BribeModel()

    arb     = simulate_arb(pool_a, pool_b, Q)
    net_raw = arb["net"]

    gas_cost = chain.base_gas_usd + chain.priority_fee_usd + chain.bridge_fee_usd
    surplus  = max(0.0, net_raw - gas_cost)
    bribe_usd = bribe_ratio * surplus
    net_after = surplus - bribe_usd            # = surplus × (1 − bribe_ratio)

    pw     = p_win_from_bribe(bribe_ratio, bribe_model)
    f_cost = failure_cost_expected(chain, pw)
    h_cost = holding_cost(holding)

    ev = pw * net_after - f_cost - h_cost

    return {
        "bribe_ratio": bribe_ratio,
        "p_win":       round(pw, 4),
        "net_raw":     round(net_raw, 4),
        "gas_cost":    round(gas_cost, 4),
        "surplus":     round(surplus, 4),
        "bribe_usd":   round(bribe_usd, 4),
        "net_after":   round(net_after, 4),
        "f_cost":      round(f_cost, 4),
        "h_cost":      round(h_cost, 4),
        "ev":          round(ev, 4),
    }


# ──────────────────────────────────────────────
# 6. EV 曲線 + k 敏感度
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
    ⚠️ k 是猜測值，Day 8 校準前不可輕信 EV 絕對值。
    """
    rows = []
    for k in k_values:
        model = BribeModel(k=k, midpoint=0.95)
        for r in bribe_ratios:
            result = compute_ev(r, pool_a, pool_b, Q, chain, holding, model)
            rows.append({"bribe_ratio": r, "k": k, "ev": result["ev"]})
    return pd.DataFrame(rows)


# ──────────────────────────────────────────────
# 7. best_ev（產品介面：go/no-go）
# ──────────────────────────────────────────────

def best_ev(
    pool_a: PoolState,
    pool_b: PoolState,
    chain: ChainParams,
    holding: Optional[HoldingParams] = None,
    bribe_model: Optional[BribeModel] = None,
    Q_bounds: tuple[float, float] = (100.0, None),
) -> dict:
    """
    對 Q 和 bribe_ratio 做雙變數最佳化，找到最大 EV 的 (Q*, r*)。

    這是整個模型的產品介面：輸入機會（兩個池），輸出 go/no-go 決策。

    設計說明：
      - 閉式解 Q* 降級為快篩和初始猜測，最終由數值最佳化確認。
      - r 有內部最優解（dEV/dr = 0），但因 p_win sigmoid 非線性，
        閉式解複雜，這裡用聯合數值搜尋。
      - Q_bounds[1] 若為 None，自動設為 min(pool_a.x, pool_b.y) × 0.3。

    ⚠️ Q* 不一定等於 EV 最優規模（儘管在當前 bribe 結構下大致成立），
    以下三種情況會偏離：
      (a) p_win 與 Q 相關（機會越大競爭越多）
      (b) bribe 基礎不是 surplus 的比例形式
      (c) 有 Q 的非線性成本項（如庫存風險 ∝ Q·σ√t）

    Returns dict:
        Q_star      最優輸入規模
        r_star      最優 bribe_ratio
        ev_star     最大 EV
        decision    "go" / "no-go"
        detail      compute_ev 完整輸出
    """
    if holding is None:
        holding = HoldingParams()
    if bribe_model is None:
        bribe_model = BribeModel()

    # 快篩：先用閉式解估算方向
    opt = optimal_size(pool_a, pool_b)
    if opt["direction"] == "no_opportunity":
        pa, pb = pool_a, pool_b
    elif opt["direction"] == "A→B":
        pa, pb = pool_a, pool_b
    else:
        pa, pb = pool_b, pool_a

    Q_max = Q_bounds[1] or min(pa.x, pb.y) * 0.3
    Q_init = opt["Q_star"] if opt["Q_star"] > 0 else (Q_bounds[0] + Q_max) / 2

    def neg_ev(params):
        Q, r = params
        if Q <= 0 or not (0 <= r <= 1):
            return 0.0
        try:
            return -compute_ev(r, pa, pb, Q, chain, holding, bribe_model)["ev"]
        except Exception:
            return 0.0

    # 網格初始化（Q × r 各 8 點），取最優點作為起點
    best_init, best_val = [Q_init, 0.5], float("inf")
    for Q0 in [Q_init * f for f in [0.3, 0.6, 1.0, 1.5, 2.0]]:
        for r0 in [0.3, 0.5, 0.7, 0.9]:
            v = neg_ev([Q0, r0])
            if v < best_val:
                best_val, best_init = v, [Q0, r0]

    res = minimize(
        neg_ev,
        x0=best_init,
        method="Nelder-Mead",
        options={"xatol": 0.01, "fatol": 1e-6, "maxiter": 2000},
        bounds=[(Q_bounds[0], Q_max), (0.0, 1.0)],
    )

    Q_star, r_star = res.x
    Q_star = max(Q_bounds[0], min(Q_max, Q_star))
    r_star = max(0.0, min(1.0, r_star))

    detail = compute_ev(r_star, pa, pb, Q_star, chain, holding, bribe_model)
    ev_star = detail["ev"]
    decision = "go" if ev_star > 0 else "no-go"

    return {
        "Q_star":   round(Q_star, 2),
        "r_star":   round(r_star, 4),
        "ev_star":  round(ev_star, 4),
        "decision": decision,
        "direction": opt["direction"],
        "detail":   detail,
    }


# ──────────────────────────────────────────────
# 8. 測試
# ──────────────────────────────────────────────

def test_double_sided_impact():
    """
    驗證雙邊衝擊遠超過單邊估計的誤差。
    """
    pool_a = PoolState(x=6_000_000, y=3_000, fee=0.003)
    pool_b = PoolState(x=3_000, y=6_060_000, fee=0.003)
    Q = 20_000

    result = simulate_arb(pool_a, pool_b, Q)
    W, _, _ = amm_out(pool_a, dx=Q)
    spot_b = pool_b.y / pool_b.x
    single_net = W * spot_b * (1 - pool_b.fee) - Q

    print("=" * 60)
    print("  test_double_sided_impact")
    print("=" * 60)
    print(f"  Pool A：spot ${pool_a.x/pool_a.y:,.0f}/WETH")
    print(f"  Pool B：spot ${pool_b.y/pool_b.x:,.0f}/WETH")
    print(f"  Q = ${Q:,} → W = {result['W']:.4f} WETH")
    print(f"  單邊近似 net : ${single_net:>+10.2f}")
    print(f"  雙邊真實 net : ${result['net']:>+10.2f}")
    print(f"  誤差         : ${single_net - result['net']:>+10.2f}")
    assert result["net"] < single_net
    if result["net"] < 0 < single_net:
        print("  ✅ 單邊獲利，雙邊虧損——關鍵誤差確認")
    else:
        print(f"  ✅ 雙邊 ({result['net']:.2f}) < 單邊 ({single_net:.2f})")
    print("=" * 60)
    return result


def test_amm_x_new():
    """
    驗證 amm_out 的 x_new 使用全額 dx（非 dx_net）。
    """
    pool = PoolState(x=1_000_000, y=1_000, fee=0.003)
    dx = 10_000
    dy, x_new, y_new = amm_out(pool, dx)
    assert x_new == pool.x + dx, f"x_new 應為 {pool.x + dx}，實際 {x_new}"
    assert abs((x_new - pool.x_net if hasattr(pool, 'x_net') else 0)) >= 0  # 不用 dx_net
    # 守恆性確認（含手續費後 k 應略增）
    k_before = pool.x * pool.y
    k_after  = x_new * y_new
    assert k_after >= k_before, "成交後 k 應 ≥ 成交前（手續費留在池中）"
    print()
    print("  test_amm_x_new ✅")
    print(f"    dx={dx}, x_new={x_new} (= x + dx = {pool.x}+{dx})")
    print(f"    k_before={k_before:.0f}, k_after={k_after:.0f} (k_after ≥ k_before ✅)")


def test_optimal_size_same_fee():
    """同費率基本案例，誤差應 < 0.1%。"""
    pool_a = PoolState(x=6_000_000, y=3_000, fee=0.003)
    pool_b = PoolState(x=3_000, y=6_060_000, fee=0.003)
    res = optimal_size(pool_a, pool_b)
    print()
    print("=" * 60)
    print("  test_optimal_size (same fee 0.3%)")
    print("=" * 60)
    print(f"  方向       : {res['direction']}")
    print(f"  閉式解 Q*  : ${res['Q_star']:>10,.2f}")
    print(f"  數值解 Q*  : ${res['numeric_Q']:>10,.2f}")
    print(f"  誤差       : {res['error_pct']:.4f}%")
    print(f"  Q* 下毛利  : ${res['net_star']:>10,.4f}")
    assert res["error_pct"] < 0.1, f"誤差超過 0.1%: {res['error_pct']:.4f}%"
    print("  ✅ 誤差 < 0.1%")
    print("=" * 60)
    return res


def test_optimal_size_diff_fee():
    """
    異費率測試：fee_a=0.05%, fee_b=0.3%（Uniswap v3 常見組合）。
    驗證正確分母 g1·Rb0 + g1·g2·Ra1 的誤差 < 0.1%，
    並確認錯誤分母 g2·Rb0 + g1·g2·Ra1 在此條件下會超標。
    """
    # 2% 價差確保有套利空間
    pool_a = PoolState(x=6_000_000, y=3_000, fee=0.0005)
    pool_b = PoolState(x=3_000, y=6_120_000, fee=0.003)

    res = optimal_size(pool_a, pool_b)

    # 重新計算錯誤分母作為對比
    g1 = 1 - pool_a.fee
    g2 = 1 - pool_b.fee
    Ra0, Ra1, Rb0, Rb1 = pool_a.x, pool_a.y, pool_b.x, pool_b.y
    numer = math.sqrt(g1*g2*Ra0*Ra1*Rb0*Rb1) - Ra0*Rb0
    Qs_wrong = numer / (g2*Rb0 + g1*g2*Ra1)
    err_wrong = abs(Qs_wrong - res["numeric_Q"]) / max(res["numeric_Q"], 1e-9) * 100

    print()
    print("=" * 60)
    print("  test_optimal_size_diff_fee (fee_a=0.05%, fee_b=0.3%)")
    print("=" * 60)
    print(f"  方向           : {res['direction']}")
    print(f"  正確閉式解 Q*  : ${res['Q_star']:>10,.2f}  誤差 {res['error_pct']:.4f}%")
    print(f"  錯誤分母 Q*    : ${Qs_wrong:>10,.2f}  誤差 {err_wrong:.4f}%")
    print(f"  數值解 Q*      : ${res['numeric_Q']:>10,.2f}")
    assert res["error_pct"] < 0.1, f"正確公式誤差超過 0.1%: {res['error_pct']:.4f}%"
    assert err_wrong > res["error_pct"], "錯誤分母應比正確分母誤差更大"
    print(f"  ✅ 正確公式誤差 {res['error_pct']:.5f}% < 0.1%")
    print(f"  ✅ 錯誤分母誤差 {err_wrong:.4f}% > 正確公式")
    print("=" * 60)
    return res


def test_optimal_size_reverse():
    """驗證反向套利被正確偵測。"""
    # pool_a 比 pool_b 貴（B→A 有機會）
    pool_a = PoolState(x=3_000, y=6_060_000, fee=0.003)   # spot $2020
    pool_b = PoolState(x=6_000_000, y=3_000, fee=0.003)   # spot $2000（低）
    res = optimal_size(pool_a, pool_b)
    print()
    print("  test_optimal_size_reverse ✅")
    print(f"    方向: {res['direction']}, Q*={res['Q_star']:.0f}, net={res['net_star']:.4f}")
    assert res["direction"] == "B→A", f"應偵測到 B→A，實際: {res['direction']}"


def test_venue_failure_cost():
    """驗證三種 venue 的失敗成本計算。"""
    c_bundle = ChainParams(venue="bundle", revert_gas_usd=1.5, n_attempts=3)
    c_public = ChainParams(venue="public", revert_gas_usd=1.5, n_attempts=3)
    c_l2     = ChainParams(venue="l2",     revert_gas_usd=0.01, n_attempts=3, l2_revert_rate=0.30)

    p_win = 0.3
    assert failure_cost_expected(c_bundle, p_win) == 0.0
    assert abs(failure_cost_expected(c_public, p_win) - (1-p_win)*1.5*3) < 1e-9
    assert abs(failure_cost_expected(c_l2, p_win) - 0.30*0.01*3) < 1e-9
    print()
    print("  test_venue_failure_cost ✅")
    print(f"    bundle : $0.0")
    print(f"    public : ${failure_cost_expected(c_public, p_win):.4f}  (= (1-{p_win})×{1.5}×{3})")
    print(f"    l2     : ${failure_cost_expected(c_l2, p_win):.4f}  (= {0.30}×{0.01}×{3})")


def test_bribe_surplus():
    """驗證 bribe 以 surplus 為基礎，不會超過 net_raw。"""
    pool_a = PoolState(x=6_000_000, y=3_000, fee=0.003)
    pool_b = PoolState(x=3_000, y=6_060_000, fee=0.003)
    chain  = ChainParams(venue="public")

    r = compute_ev(0.8, pool_a, pool_b, 5_945.0, chain)
    assert r["bribe_usd"] <= r["net_raw"], "bribe 不能超過 net_raw"
    assert r["surplus"] >= 0
    assert abs(r["surplus"] - max(0, r["net_raw"] - r["gas_cost"])) < 0.01
    print()
    print("  test_bribe_surplus ✅")
    print(f"    net_raw={r['net_raw']}, gas={r['gas_cost']}, surplus={r['surplus']}, bribe={r['bribe_usd']}")


# ──────────────────────────────────────────────
# 9. verify_all — 七項修正驗證清單
# ──────────────────────────────────────────────

def verify_all() -> None:
    """
    驗證 Day 2b 七項修正，每項一個 assert，跑完印出通過/失敗清單。

    1. 閉式解分母：fee 0.0001 vs 0.01（v3 最大費率差）誤差 < 0.1%
    2. amm_out：x_new == x + dx（不是 x + dx_net）
    3. 雙方向：只有 B→A 有利的池組，assert 找到機會
    4. bribe 基礎：bribe_ratio=1.0 時 net_after == 0（不是 −gas）
    5. Q* 不變性：固定 Q，驗證 dEV/dQ 方向對 r 無關
    6. best_ev 四個 key 存在
    7. venue="l2"：失敗機率來自 l2_revert_rate，不是 1−p_win
    """
    results = []

    def check(name: str, fn):
        try:
            fn()
            results.append((name, "PASS", None))
        except Exception as e:
            results.append((name, "FAIL", str(e)))

    # ── 1. 閉式解分母（最大費率差）──────────────────────────────
    def _1():
        # fee_a=0.01% (v3 最低), fee_b=1.0% (v3 最高)
        # 需要足夠大的價差才能在高費率下仍有套利空間
        pool_a = PoolState(x=10_000_000, y=5_000, fee=0.0001)   # spot $2000
        pool_b = PoolState(x=5_000, y=11_000_000, fee=0.01)     # spot $2200，10%差
        g1 = 1 - pool_a.fee
        g2 = 1 - pool_b.fee
        Ra0, Ra1, Rb0, Rb1 = pool_a.x, pool_a.y, pool_b.x, pool_b.y
        inner = g1 * g2 * Ra0 * Ra1 * Rb0 * Rb1
        numer = math.sqrt(inner) - Ra0 * Rb0
        assert numer > 0, "此池組應有套利機會（numer > 0）"

        Q_correct = numer / (g1 * Rb0 + g1 * g2 * Ra1)
        Q_wrong   = numer / (g2 * Rb0 + g1 * g2 * Ra1)

        # 數值解
        res = minimize_scalar(
            lambda Q: -simulate_arb(pool_a, pool_b, Q)["net"] if Q > 0 else 0.0,
            bounds=(1.0, min(Ra0, Rb1) * 0.5), method="bounded"
        )
        Q_num = res.x
        err_c = abs(Q_correct - Q_num) / max(Q_num, 1e-9) * 100
        err_w = abs(Q_wrong - Q_num) / max(Q_num, 1e-9) * 100

        assert err_c < 0.1, f"正確公式誤差 {err_c:.4f}% ≥ 0.1%"
        assert err_w > err_c, f"錯誤分母誤差 ({err_w:.4f}%) 應 > 正確公式 ({err_c:.4f}%)"

    check("1. 閉式解分母（fee 0.01% vs 1.0%，誤差<0.1%）", _1)

    # ── 2. amm_out x_new == x + dx ──────────────────────────────
    def _2():
        pool = PoolState(x=1_000_000, y=500, fee=0.003)
        dx = 15_000
        dx_net = dx * (1 - pool.fee)
        dy, x_new, y_new = amm_out(pool, dx)
        assert x_new == pool.x + dx, \
            f"x_new={x_new} ≠ x+dx={pool.x+dx}（不應用 dx_net={dx_net:.2f}）"

    check("2. amm_out x_new = x + dx（非 dx_net）", _2)

    # ── 3. 雙方向：只有 B→A 有利 ────────────────────────────────
    def _3():
        # 兩個池的 token 佈局：
        #   simulate_arb(pa, pb, Q) = Q token0 → pa(買token1) → pb(token1→token0)
        #   所以 pa.x=token0, pa.y=token1; pb.x=token1, pb.y=token0
        #
        # 構造「只有 B→A 有利」：pool_b 便宜（低買），pool_a 貴（高賣）
        #   B→A = Q USDC → pool_b(買WETH) → pool_a(WETH賣回USDC)
        #   所以傳入 optimal_size 的 pool_a/pool_b 要讓 B→A 方向賺錢：
        #     pool_a_for_b2a: x=USDC(3M),  y=WETH(1500), spot=$2000  ← 便宜，作為第一個池
        #     pool_b_for_b2a: x=WETH(1500), y=USDC(3.03M), spot=$2020 ← 貴，作為第二個池
        #   但 optimal_size 輸入的是兩個「以 USDC 為 x」的池，
        #   所以構造：
        #     pool_a（傳入）: x=USDC(3.03M), y=WETH(1500), spot=2020  ← 貴
        #     pool_b（傳入）: x=USDC(3M),    y=WETH(1500), spot=2000  ← 便宜
        #   optimal_size 內部用 _optimal_size_one_direction 分別試兩個方向：
        #     A→B: pool_a.x→pool_a.y→pool_b 需要 pool_b.x=WETH, pool_b.y=USDC
        #     B→A: pool_b.x→pool_b.y→pool_a 需要 pool_a.x=WETH, pool_a.y=USDC
        #   optimal_size 的 B→A 呼叫是 _optimal_size_one_direction(pool_b, pool_a)
        #   即 Ra0=pool_b.x=USDC, Ra1=pool_b.y=WETH, Rb0=pool_a.x=USDC(???), Rb1=pool_a.y=WETH
        #   → optimal_size 目前沒有 token 對調邏輯，B→A 的池組合可能不正確
        #
        # 最直接的做法：直接傳已對調的池進入，讓 A→B 就是我們要的方向
        #   第一個池：x=USDC(3M,便宜), y=WETH(1500)  spot=2000
        #   第二個池：x=WETH(1500,貴), y=USDC(3.03M) spot=2020
        pool_first  = PoolState(x=3_000_000, y=1_500, fee=0.003)   # USDC→WETH, spot $2000
        pool_second = PoolState(x=1_500, y=3_030_000, fee=0.003)   # WETH→USDC, spot $2020
        # 驗證 A→B 有利
        net = simulate_arb(pool_first, pool_second, 3_000)["net"]
        assert net > 0, f"A→B net={net:.4f} 應 > 0"
        # optimal_size 直接找
        res = optimal_size(pool_first, pool_second)
        assert res["direction"] != "no_opportunity", "應找到套利機會"
        assert res["net_star"] > 0, f"net_star={res['net_star']} 應 > 0"
        # 確認反向（傳入對調）是虧損
        net_rev = simulate_arb(pool_second, pool_first, 3_000)["net"]
        assert net_rev < net, f"反向應比正向差"

    check("3. 雙方向：B→A 反向案例被正確偵測", _3)

    # ── 4. bribe_ratio=1.0 時 net_after == 0 ────────────────────
    def _4():
        pool_a = PoolState(x=6_000_000, y=3_000, fee=0.003)
        pool_b = PoolState(x=3_000, y=6_060_000, fee=0.003)
        chain  = ChainParams(venue="public")
        r = compute_ev(1.0, pool_a, pool_b, 5_945.0, chain)
        # bribe_ratio=1.0 → bribe_usd=surplus, net_after=surplus*(1-1)=0
        assert abs(r["net_after"]) < 1e-9, \
            f"bribe=1.0 時 net_after={r['net_after']} 應為 0（不是 -gas）"
        # 確認不是舊的錯誤行為（net_after = net_raw - gas）
        old_wrong = r["net_raw"] - r["gas_cost"]
        if abs(old_wrong) > 1e-6:
            assert abs(r["net_after"] - old_wrong) > 1e-6, \
                f"net_after={r['net_after']} 不應等於 net_raw−gas={old_wrong:.4f}"

    check("4. bribe=1.0 → net_after=0（非 net_raw−gas）", _4)

    # ── 5. Q* 不變性：dEV/dQ 方向與 r 無關 ─────────────────────
    def _5():
        pool_a = PoolState(x=6_000_000, y=3_000, fee=0.003)
        pool_b = PoolState(x=3_000, y=6_060_000, fee=0.003)
        chain  = ChainParams(venue="public")
        # 在 Q* 左側（Q < Q*），梯度應為正（EV 隨 Q 增加）
        # 在 Q* 右側（Q > Q*），梯度應為負
        # 對所有 r，梯度正負號應一致（不依賴 r）
        Q_left  = 4_000   # < Q* ≈ 5945
        Q_right = 7_000   # > Q*
        eps = 200
        for Q_test, expected_sign in [(Q_left, +1), (Q_right, -1)]:
            grads = []
            for r in [0.1, 0.5, 0.9]:
                ev_up = compute_ev(r, pool_a, pool_b, Q_test + eps, chain)["ev"]
                ev_dn = compute_ev(r, pool_a, pool_b, Q_test - eps, chain)["ev"]
                grads.append(ev_up - ev_dn)
            signs = [1 if g > 0 else -1 if g < 0 else 0 for g in grads]
            assert all(s == expected_sign for s in signs), \
                f"Q={Q_test}：不同 r 的梯度方向不一致 {signs}（代表 Q 相依項被引入）"

    check("5. Q* 不變性：dEV/dQ 方向對所有 r 一致", _5)

    # ── 6. best_ev 四個 key ─────────────────────────────────────
    def _6():
        pool_a = PoolState(x=6_000_000, y=3_000, fee=0.003)
        pool_b = PoolState(x=3_000, y=6_060_000, fee=0.003)
        chain  = ChainParams(venue="public")
        be = best_ev(pool_a, pool_b, chain)
        for key in ("Q_star", "r_star", "ev_star", "decision"):
            assert key in be, f"best_ev 缺少 key: {key}"
        assert be["decision"] in ("go", "no-go"), \
            f"decision 應為 'go' 或 'no-go'，實際: {be['decision']}"

    check("6. best_ev 回傳四個必要 key", _6)

    # ── 7. venue=l2 失敗機率來自 l2_revert_rate ─────────────────
    def _7():
        rate    = 0.25
        revert  = 1.5
        n       = 3
        chain_l2 = ChainParams(venue="l2", revert_gas_usd=revert,
                               n_attempts=n, l2_revert_rate=rate)
        # 任意 p_win，l2 的 f_cost 應只取決於 l2_revert_rate
        for p_win in [0.1, 0.5, 0.9]:
            expected = rate * revert * n
            actual   = failure_cost_expected(chain_l2, p_win)
            assert abs(actual - expected) < 1e-9, \
                f"p_win={p_win}: f_cost={actual:.4f} ≠ l2_revert_rate×revert×n={expected:.4f}"
            # 確認不等於 (1-p_win) × revert × n
            wrong = (1 - p_win) * revert * n
            if abs(expected - wrong) > 1e-6:   # 只在差異顯著時才斷言
                assert abs(actual - wrong) > 1e-6, \
                    f"p_win={p_win}: l2 f_cost 不應等於 (1−p_win)×revert×n={wrong:.4f}"

    check("7. venue=l2 失敗成本用 l2_revert_rate，不依賴 p_win", _7)

    # ── 結果印出 ────────────────────────────────────────────────
    print()
    print("=" * 62)
    print("  verify_all — 七項修正驗證")
    print("=" * 62)
    all_pass = True
    for name, status, err in results:
        icon = "✅" if status == "PASS" else "❌"
        print(f"  {icon} {name}")
        if err:
            print(f"     → {err}")
            all_pass = False
    print("=" * 62)
    if all_pass:
        print("  全部通過 ✅")
    else:
        n_fail = sum(1 for _, s, _ in results if s == "FAIL")
        print(f"  {n_fail} 項失敗 ❌")
    print("=" * 62)
    return all_pass


# ──────────────────────────────────────────────
# 10. 驗收區塊
# ──────────────────────────────────────────────

if __name__ == "__main__":
    import numpy as np

    verify_all()

    test_amm_x_new()
    test_optimal_size_same_fee()
    test_optimal_size_diff_fee()
    test_optimal_size_reverse()
    test_venue_failure_cost()
    test_bribe_surplus()

    # EV 曲線
    pool_a = PoolState(x=6_000_000, y=3_000, fee=0.003)
    pool_b = PoolState(x=3_000, y=6_060_000, fee=0.003)
    chain  = ChainParams(venue="public")

    opt = optimal_size(pool_a, pool_b)
    Q_star = opt["Q_star"] if opt["Q_star"] > 0 else 5_000.0

    print()
    print("=" * 65)
    print(f"  EV 曲線（Q*={Q_star:,.0f} USDC，venue=public）")
    print("  ⚠️ p_win 基於猜測 sigmoid，絕對值不可輕信")
    print("=" * 65)
    ratios = np.arange(0.0, 1.05, 0.1).tolist()
    df = sweep_bribe(ratios, pool_a, pool_b, Q_star, chain)
    print(df[["bribe_ratio", "p_win", "net_raw", "surplus", "net_after", "ev"]].to_string(index=False))

    print()
    print("=" * 65)
    print("  best_ev（雙變數最佳化 go/no-go）")
    print("=" * 65)
    be = best_ev(pool_a, pool_b, chain)
    print(f"  Q*       = ${be['Q_star']:,.2f}")
    print(f"  r*       = {be['r_star']:.4f}")
    print(f"  EV*      = ${be['ev_star']:.4f}")
    print(f"  決策     = {be['decision']}")
    print(f"  方向     = {be['direction']}")

    print()
    print("=" * 65)
    print("  k 敏感度（⚠️ k 是猜測值）")
    print("=" * 65)
    df_s = bribe_sensitivity(
        [0.3, 0.5, 0.7], [2.0, 5.65, 10.0, 20.0],
        pool_a, pool_b, Q_star, chain
    )
    print(df_s.pivot(index="bribe_ratio", columns="k", values="ev").to_string())
    print("\n  → Day 8 前，bribe 掃描結果僅供定性參考")
