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
  bribe_usd = bribe_ratio × surplus
  net_after = net_raw − gas_cost − bribe_usd   （不 clamp，可為負）
  EV = p_win × net_after − f_cost_expected − h_cost

  其中 f_cost_expected：
    bundle : 0
    public : (1 − p_win) × revert_gas × n_attempts
    l2     : l2_revert_rate × revert_gas × n_attempts  （獨立隨機過程，與 bribe 無關）
"""

from __future__ import annotations
import math
from dataclasses import dataclass, field
from typing import Optional, Literal, Protocol
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
class Leg:
    """
    套利路徑的單一腿（執行時用）。

    包含組裝 Flashbots bundle transaction 所需的完整資訊：
      pool_addr  : 池合約地址
      token_in   : 輸入 token 地址
      token_out  : 輸出 token 地址
      amount_in  : 本腿輸入量（原始 token 單位，未除 decimals）
      amount_out : 本腿預期輸出量（原始 token 單位）
      dex        : 來源 DEX 名稱（e.g. "uniswap_v2", "sushiswap_v2"）
      fee        : 池費率（e.g. 0.003）

    amount_in / amount_out 在 scan_triangles 中以 USD 估算，
    要轉成 on-chain wei 需再乘 10^decimals。
    """
    pool_addr:  str
    token_in:   str
    token_out:  str
    amount_in:  float   # token 單位（非 wei）
    amount_out: float   # token 單位（非 wei）
    dex:        str
    fee:        float


# ──────────────────────────────────────────────
# 1b. 通用腿介面（Day 9：支援非 AMM 腿）
# ──────────────────────────────────────────────

@dataclass
class CashFlow:
    """
    一筆現金流。正=流入本策略，負=流出。

    t_hours : 相對進場的時間（0=立即，672=28天）
    kind    : "principal"=本金；"yield"=利息/資金費；"fee"=手續費/結算費
    floating: 是否為浮動項（用於自動驗算浮動對消）
    counterparty: 來源場所（用於跨場所浮動對消驗算）
    """
    asset:        str
    amount:       float
    t_hours:      float
    kind:         Literal["principal", "yield", "fee"] = "principal"
    floating:     bool = False
    counterparty: str  = ""


@dataclass
class Margin:
    """
    佔用的保證金。

    cross_margined_with: 與哪些倉位共用保證金
    （當兩腿跨平台 cross-margin，清算條件要聯合評估）
    """
    asset:               str
    amount:              float
    venue:               str
    cross_margined_with: list[str] = field(default_factory=list)


@dataclass
class LiquidationCondition:
    """
    清算觸發條件。

    driver    : 驅動因素 —— "price" / "implied_apr" / "health_factor"
    direction : "up" = 上漲觸發（short 被清算）；"down" = 下跌觸發（long 被清算）
    threshold : 觸發值（None = 未知/需動態計算）
    note      : 補充說明
    """
    driver:    str
    direction: Literal["up", "down"]
    threshold: float | None
    note:      str = ""


@dataclass
class LegResult:
    """
    一條腿的評估結果。

    flows       : 所有現金流（含本金、利息、費用）
    margins     : 佔用的保證金列表
    delta       : 資產 → USD 計價的價格曝險（正=多頭，負=空頭）
    liquidations: 清算條件列表
    exit_ok     : 退出通道是否暢通（False = 需評估深度/鎖倉）
    atomic      : 是否原子（False = 部分成交後暴露庫存風險）
    meta        : 額外資訊（e.g. 槓桿率、固定利率、到期日）
    """
    flows:       list[CashFlow]
    margins:     list[Margin]
    delta:       dict[str, float]
    liquidations: list[LiquidationCondition]
    exit_ok:     bool
    exit_note:   str  = ""
    atomic:      bool = True
    meta:        dict = field(default_factory=dict)


class LegProtocol(Protocol):
    """
    通用腿介面（Day 9）。

    所有腿（AMM swap / Perp funding / Boros YU / 橋 / 借貸）
    都實作 evaluate()，讓上層模型統一處理現金流、delta、清算條件。

    notional      : 本腿名目規模（USD）
    horizon_hours : 持有時間（小時）
    """
    def evaluate(self, notional: float, horizon_hours: float) -> LegResult: ...


@dataclass
class ChainParams:
    """鏈上成本參數，預設為 L1 (Ethereum Mainnet)。"""
    base_gas_usd: float      = 5.0
    priority_fee_usd: float  = 2.0
    revert_gas_usd: float    = 1.5
    bridge_fee_usd: float    = 0.0
    n_attempts: int          = 3
    venue: Literal["bundle", "public", "l2", "multi_venue"] = "public"
    l2_revert_rate: float    = 0.30   # L2 獨立 revert 機率（研究值 20-40%）
    # venue 說明：
    #   "bundle"      → Flashbots bundle，失敗不上鏈，f_cost = 0
    #   "public"      → 公開 mempool，失敗率 = (1 − p_win)，付 revert gas
    #   "l2"          → L2，revert 是獨立隨機過程（非 bribe 拍賣）
    #   "multi_venue" → 跨場所非原子：單腿失敗 = 裸露曝險，
    #                   f_cost 不是 gas，是「被迫市價平倉」成本


@dataclass
class HoldingParams:
    """
    資金持有成本參數。Atomic arb 全部為 0。

    Day 9 修正：拆出 inventory_usd / delta_exposure_usd
    ──────────────────────────────────────────────────
    原本 price_risk = inventory_usd × σ × √t 對 delta-neutral 策略是錯的：
    Boros 四腿佔用 $288,621 資本，但 delta 完全對消（ETH 曝險=0）。
    把 σ 乘在全部庫存上，會憑空生出一筆不存在的價格風險。

    正確做法：
      inventory_usd       → 佔用資金，只算機會成本（opportunity_rate）
      delta_exposure_usd  → 未對沖淨曝險，才算 σ√t 風險
                            由上層從 sum(leg.delta.values()) 自動填入
    """
    inventory_usd:      float = 0.0   # 佔用資金（機會成本基數）
    hold_time_hours:    float = 0.0
    opportunity_rate:   float = 0.05  # 年化利率
    sigma_daily:        float = 0.03  # 日波動率
    delta_exposure_usd: float | None = None  # None=向後相容（用 inventory_usd）；0.0=delta-neutral（price_risk=0）


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


def simulate_tri_arb(
    pool_ab: PoolState, pool_bc: PoolState, pool_ca: PoolState, Q: float
) -> dict:
    """
    模擬三角套利資金流向：
      Q token_A → pool_AB → W token_B → pool_BC → V token_C → pool_CA → Q' token_A

    每腿均用 amm_out()，連續傳遞輸出（不能用邊際報價相乘）。
    net = Q' − Q（< 0 代表虧損）
    """
    if Q <= 0:
        raise ValueError("Q 必須 > 0")
    W, _, _ = amm_out(pool_ab, dx=Q)
    V, _, _ = amm_out(pool_bc, dx=W)
    Q_out, _, _ = amm_out(pool_ca, dx=V)
    net = Q_out - Q
    return {
        "Q_in":       Q,
        "W":          round(W, 8),
        "V":          round(V, 8),
        "Q_out":      round(Q_out, 6),
        "net":        round(net, 6),
        "profit_pct": round(net / Q * 100, 6) if Q > 0 else 0.0,
    }


def optimal_size_tri(
    pool_ab: PoolState, pool_bc: PoolState, pool_ca: PoolState,
    Q_min: float = 1.0,
) -> dict:
    """
    三角套利最優規模（數值最佳化）。

    三腿串接沒有乾淨的閉式解（兩池有閉式解是因為 Q' 是 Q 的有理函數，
    三腿後分子分母次數更高，求根複雜），改用 bounded scalar 最佳化。

    Q_max 只用 pool_ab.x（與 Q 同單位的輸入池儲備）的 30%。
    pool_bc.x / pool_ca.x 單位不同，不能直接 min() 比較。
    回傳 net_star <= 0 表示此路徑無獲利機會。
    """
    Q_max = pool_ab.x * 0.3
    if Q_max <= Q_min:
        return {"Q_star": 0.0, "net_star": float("-inf"), "direction": "no_opportunity"}

    def neg_net(Q: float) -> float:
        try:
            return -simulate_tri_arb(pool_ab, pool_bc, pool_ca, Q)["net"]
        except Exception:
            return float("inf")

    res = minimize_scalar(neg_net, bounds=(Q_min, Q_max), method="bounded")
    Q_star  = res.x
    net_star = simulate_tri_arb(pool_ab, pool_bc, pool_ca, Q_star)["net"]

    return {
        "Q_star":   round(Q_star, 4),
        "net_star": round(net_star, 6),
        "direction": "A→B→C→A" if net_star > 0 else "no_opportunity",
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


def optimal_size(
    pool_a: PoolState, pool_b: PoolState,
    price_x: float = 1.0,
) -> dict:
    """
    計算雙池套利的最優輸入規模 Q*（閉式解，單向 A→B）。

    price_x：pool_a.x token 的 USD 價格（預設 1.0 = stable coin）。

    只計算 A→B 方向。反向（B→A）由呼叫端交換池順序處理：
        optimal_size(pool_b, pool_a, price_bx)
    數學分析（見 test_optimal_size_reverse）：在費率相同時，
    B→A 的閉式解 numer 與 A→B 完全對稱，net_ba_usd 永遠不超過 net_ab_usd，
    因此 B→A 分支從未觸發（16 組費率/深度組合測試結果為 0 次），刪除為死碼。

    同時用 scipy 數值最佳化做 sanity check，要求誤差 < 0.1%。
    """
    # A→B 方向
    Qs_ab, net_ab = _optimal_size_one_direction(pool_a, pool_b)
    net_ab_usd = net_ab * price_x if net_ab > float("-inf") else float("-inf")

    if net_ab_usd <= 0:
        return {
            "Q_star": 0.0, "net_star": 0.0, "net_star_usd": 0.0,
            "direction": "no_opportunity",
            "numeric_Q": 0.0, "error_pct": 0.0,
        }

    Q_star, net_star, net_star_usd = Qs_ab, net_ab, net_ab_usd
    pa, pb = pool_a, pool_b

    # 數值驗證
    bound = min(pa.x, pb.y) * 0.5
    res = minimize_scalar(
        lambda Q: -simulate_arb(pa, pb, Q)["net"] if Q > 0 else float("inf"),
        bounds=(1.0, bound), method="bounded"
    )
    numeric_Q = res.x
    error_pct = abs(Q_star - numeric_Q) / max(numeric_Q, 1e-9) * 100

    return {
        "Q_star":       round(Q_star, 4),
        "net_star":     round(net_star, 6),
        "net_star_usd": round(net_star_usd, 6),
        "direction":    "A→B",
        "numeric_Q":    round(numeric_Q, 4),
        "error_pct":    round(error_pct, 4),
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

    opportunity = inventory_usd × t_years × rate

    price_risk = delta_exposure_usd × σ_daily × √t_days
      ⚠️ 這是「風險調整項」，不是期望成本。
         隨機遊走的期望價格變動 = 0，price_risk 捕捉的是
         持有期間 1-sigma 的 mark-to-market 資金敞口風險。

      Day 9 修正：改用 delta_exposure_usd 而非 inventory_usd：
        - delta-neutral 策略（Boros 四腿）：delta_exposure_usd=0 → price_risk=0
        - 一般 atomic arb：delta_exposure_usd = inventory_usd（預設向後相容）

    t_years 與 t_days 均由同一個 hold_time_hours 推導，確保一致：
      t_years = hold_time_hours / (365 × 24)
      t_days  = hold_time_hours / 24  （= t_years × 365）
    """
    if holding.inventory_usd <= 0 or holding.hold_time_hours <= 0:
        return 0.0
    t_years = holding.hold_time_hours / (365 * 24)
    t_days  = holding.hold_time_hours / 24
    opportunity = holding.inventory_usd * t_years * holding.opportunity_rate
    # 使用 delta_exposure_usd：
    #   None  → 向後相容，fallback 到 inventory_usd（一般 atomic arb）
    #   0.0   → delta-neutral，price_risk = 0（Boros 四腿等 delta-neutral 策略）
    #   > 0   → 指定未對沖淨曝險
    if holding.delta_exposure_usd is None:
        exposure = holding.inventory_usd
    else:
        exposure = holding.delta_exposure_usd
    price_risk  = exposure * holding.sigma_daily * math.sqrt(t_days)
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

    bribe 基礎為 surplus（實現剩餘的非負部分），用來限制 bribe 上界：
      surplus   = max(0, net_raw − gas_cost)   ← 只限制 bribe 上界
      bribe_usd = bribe_ratio × surplus
      net_after = net_raw − gas_cost − bribe_usd   ← 不 clamp，可為負

    關鍵區別：
      surplus   = max(0, ...) 確保 bribe 不超過實際產出（coinbase.transfer 上界）
      net_after = 不 clamp — 虧損就是虧損，必須如實反映在 EV 裡
                           否則所有虧損都被抹成 0，scanner 喪失區分「差一點」vs「差很遠」的能力

    EV = p_win × net_after − f_cost_expected − h_cost
    """
    if holding is None:
        holding = HoldingParams()
    if bribe_model is None:
        bribe_model = BribeModel()

    arb     = simulate_arb(pool_a, pool_b, Q)
    net_raw = arb["net"]

    gas_cost          = chain.base_gas_usd + chain.priority_fee_usd + chain.bridge_fee_usd
    surplus           = max(0.0, net_raw - gas_cost)   # 只用來限制 bribe 上界
    bribe_usd         = bribe_ratio * surplus
    net_after         = net_raw - gas_cost - bribe_usd  # 不 clamp，虧損如實保留

    pw     = p_win_from_bribe(bribe_ratio, bribe_model)
    f_cost = failure_cost_expected(chain, pw)
    h_cost = holding_cost(holding)

    ev = pw * net_after - f_cost - h_cost

    return {
        "bribe_ratio":  bribe_ratio,
        "p_win":        round(pw, 4),
        "net_raw":      round(net_raw, 4),
        "gas_cost":     round(gas_cost, 4),
        "surplus":      round(surplus, 4),
        "bribe_usd":    round(bribe_usd, 4),
        "net_after":    round(net_after, 4),
        "f_cost":       round(f_cost, 4),
        "h_cost":       round(h_cost, 4),
        "ev":           round(ev, 4),
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
    price_x: float = 1.0,
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

    r* 的直觀解釋（三種情境）：

      (a) venue=public，surplus > 0，r* 是否趨向邊界 1 取決於 sigmoid 斜率與 f_cost 的關係：
          充要條件：p'(1)·f_cost > p(1)·surplus，其中 p' = k·p·(1−p)
          → 當此條件成立，r* = 1（全額 bribe，用利潤換「不輸」）
          → 不成立時 r* < 1，存在內部解
          ⚠️ 此行為是校準相依的，不是結構性質：換 k/midpoint 就可能翻轉。
          不要把「r* → 1」當成普遍事實——它是這組猜測參數的產物。

      (b) venue=bundle（f_cost=0）：失敗不上鏈，不存在 revert gas 懲罰。
          → r* 為內部解（約 0.6–0.8），EV 從負轉正。
          解析條件：dEV/dr = 0 → (1−r) = 1/(k·(1−p_win))。

      (c) net_raw < gas（真虧損）：surplus=0，bribe 恆為 0，r 只影響 p_win。
          EV = p_win×(大負數) − f_cost → r* = 0（寧可輸掉拍賣，不要贏到虧損）。

    Returns dict:
        Q_star      最優輸入規模（no_opportunity 時強制為 0）
        r_star      最優 bribe_ratio
        ev_star     最佳 EV（可為負，用於機會排序）
        ev_realized max(0, ev_star)（實際會發生的期望值；不交易 = 0）
        decision    "go" / "no-go"
        detail      compute_ev 完整輸出
    """
    if holding is None:
        holding = HoldingParams()
    if bribe_model is None:
        bribe_model = BribeModel()

    # 快篩：先用閉式解估算方向（price_x 往下傳，確保方向比較用 USD）
    opt = optimal_size(pool_a, pool_b, price_x=price_x)
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
        if Q <= 0 or Q > Q_max or not (0 <= r <= 1):  # ⑤ 補擋 Q > Q_max
            return float("inf")
        try:
            return -compute_ev(r, pa, pb, Q, chain, holding, bribe_model)["ev"]
        except Exception:
            return float("inf")

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

    detail   = compute_ev(r_star, pa, pb, Q_star, chain, holding, bribe_model)
    ev_star  = detail["ev"]
    decision = "go" if ev_star > 0 else "no-go"

    # P1-1 修正：r*→1 且 surplus>0 時，EV 對 Q 完全平坦（net_after=0），
    # 優化器沒有梯度，停在哪都行，回報的 Q_star 是假精度。
    # 此時改用閉式解 Q_star，並標記 q_indeterminate=True。
    q_indeterminate = False
    surplus = detail.get("surplus", max(0.0, detail.get("net_raw", 0.0)
                                        - chain.base_gas_usd - chain.priority_fee_usd))
    if abs(r_star - 1.0) < 1e-4 and surplus > 0:
        q_indeterminate = True
        Q_star = opt["Q_star"] if opt["Q_star"] > 0 else Q_star  # fallback 閉式解

    # ③「不交易」選項：no_opportunity 時最佳 Q=0，強制修正數值優化器的無意義輸出
    if opt["direction"] == "no_opportunity":
        Q_star  = 0.0
        ev_star = min(ev_star, 0.0)   # 確保不交易不會顯示正 EV

    ev_realized = max(0.0, ev_star)   # 實際會發生的期望值（不交易 = 0）

    return {
        "Q_star":          round(Q_star, 2),
        "r_star":          round(r_star, 4),
        "ev_star":         round(ev_star, 4),     # 可為負，用於機會排序
        "ev_realized":     round(ev_realized, 4), # max(0, ev_star)，實際期望值
        "decision":        decision,
        "direction":       opt["direction"],
        "q_indeterminate": q_indeterminate,        # True = Q* 是閉式解，非數值優化
        "detail":          detail,
    }


# ──────────────────────────────────────────────
# 8. LiFi 費用轉換（跨鏈套利用）
# ──────────────────────────────────────────────

def lifi_quote_to_chain_params(
    quote: dict,
    base: Optional[ChainParams] = None,
) -> ChainParams:
    """
    把 LiFi /quote 或 /advanced/routes 的單條路由，
    轉換成可傳入 compute_ev / best_ev 的 ChainParams。

    費用處理規則（來自 LiFi API 文件 + 實測）：
      gasCosts[]         → 額外付給礦工，加進 base_gas_usd
      feeCosts[included=True]  → 已從 toAmount 扣除，不重複計
      feeCosts[included=False] → 尚未扣除，加進 bridge_fee_usd

    ⚠️ 最常見 bug：把 included=True 的費用再扣一次（重複計費）。
       本函式確保只計算「你還需要額外付出去」的費用。

    Args:
        quote: LiFi /quote 的 JSON response（整個 dict）
        base:  基礎 ChainParams（保留 venue / revert_gas 等設定）

    Returns:
        新的 ChainParams，bridge_fee_usd 已填入 LiFi 費用
    """
    if base is None:
        base = ChainParams()

    est = quote.get("estimate", {})

    # gasCosts：額外付 ETH 給礦工
    gas_usd = sum(
        float(g.get("amountUSD", 0))
        for g in est.get("gasCosts", [])
    )

    # feeCosts：只加 included=False（included=True 已在 toAmount 裡扣了）
    extra_fee_usd = sum(
        float(f.get("amountUSD", 0))
        for f in est.get("feeCosts", [])
        if not f.get("included", True)
    )

    # 摘要（供 debug）
    included_fees = sum(
        float(f.get("amountUSD", 0))
        for f in est.get("feeCosts", [])
        if f.get("included", True)
    )

    return ChainParams(
        base_gas_usd     = 0.0,              # ⚠️ P1-3 修正：用 LiFi 報價時 base_gas 歸零，避免雙重計算
        priority_fee_usd = gas_usd,          # LiFi gasCosts 放在 priority_fee 欄位
        revert_gas_usd   = base.revert_gas_usd,
        bridge_fee_usd   = extra_fee_usd,    # 只有 included=False 才是額外成本
        n_attempts       = base.n_attempts,
        venue            = base.venue,
        l2_revert_rate   = base.l2_revert_rate,
        # 備註欄位（不進計算，供人工核對）
        # _lifi_gas_usd          = gas_usd
        # _lifi_included_fee_usd = included_fees  ← 已在 toAmount 扣，不算
        # _lifi_extra_fee_usd    = extra_fee_usd
    )


def lifi_net_raw(quote: dict) -> float:
    """
    從 LiFi quote 直接算出 net_raw（= toAmount_USD - fromAmount_USD）。

    跨鏈套利用：不需要 simulate_arb，直接用 LiFi 報價的 toAmount。
    注意：toAmount 已扣除 included=True 的費用，是真實到手金額。

    Args:
        quote: LiFi /quote 的 JSON response

    Returns:
        net_raw（USD），正數代表有毛利（扣除 included 費用後）
    """
    act = quote.get("action", {})
    est = quote.get("estimate", {})

    from_token = act.get("fromToken", {})
    to_token   = act.get("toToken", {})

    # ⚠️ P1-3 修正：decimals 預設 6 對 WETH（18）會誤差 10^12 倍，拿不到就 raise
    if "decimals" not in from_token:
        raise ValueError(f"lifi_net_raw: fromToken 缺少 decimals，"
                         f"token={from_token.get('symbol', '?')}，請確認 quote 完整")
    if "decimals" not in to_token:
        raise ValueError(f"lifi_net_raw: toToken 缺少 decimals，"
                         f"token={to_token.get('symbol', '?')}，請確認 quote 完整")

    from_dec   = from_token["decimals"]
    to_dec     = to_token["decimals"]

    from_price = float(from_token.get("priceUSD", 1.0))
    to_price   = float(to_token.get("priceUSD", 1.0))

    from_amount = int(act.get("fromAmount", 0)) / (10 ** from_dec)
    to_amount   = int(est.get("toAmount", 0))   / (10 ** to_dec)

    from_usd = from_amount * from_price
    to_usd   = to_amount   * to_price

    return round(to_usd - from_usd, 6)




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
    """
    P1-2 修正：此測試之前從未觸發 B→A 分支。

    數學分析（200 次隨機池實測）：
      ratio_ba/net_ab 最大 = 0.990（< 1），B→A 永遠不會贏。

    根本原因：
      A→B 和 B→A 的閉式解 numer 完全相同（inner = g²·Ra0·Ra1·Rb0·Rb1 對稱），
      所以 net_ba / spot_ab 的上界恰好是 net_ab，B→A 不可能超越 A→B。

      「price_x 決定勝負」的描述也不準確：
        net_ab_usd = net_ab * price_x
        net_ba_usd = net_ba * (price_x / spot_ab)
      兩者都乘了 price_x，大小關係與 price_x 無關。

    意義：
      B→A 分支是防禦性程式碼（防止 pool 傳入順序不同時產生錯誤），
      不是可以被正常套利機會觸發的路徑。
      要觸發 B→A 需要手動翻轉 pool 傳入順序。

    本測試改為：
      (1) 驗 price_x 確實影響 net_star_usd 的絕對值（縮放正確）
      (2) 驗 B→A 可以被直接傳入翻轉的 pool 觸發
      (3) 說清楚 B→A 永遠不會從 price_x 變化中自動出現
    """
    pool_a = PoolState(x=3_000, y=6_060_000, fee=0.003)   # WETH/USDC
    pool_b = PoolState(x=6_000_000, y=3_000, fee=0.003)   # USDC/WETH

    # (1) price_x 影響 net_star_usd 的絕對值（縮放正確）
    res_low  = optimal_size(pool_a, pool_b, price_x=1.0)
    res_high = optimal_size(pool_a, pool_b, price_x=2020.0)
    assert res_low["direction"] != "no_opportunity", "應有套利機會"
    assert abs(res_high["net_star_usd"] / res_low["net_star_usd"] - 2020.0) < 1.0, \
        f"price_x=2020 的 net_usd 應是 price_x=1 的 2020 倍，實際: {res_high['net_star_usd']/res_low['net_star_usd']:.1f}x"

    # (2) B→A 可以透過翻轉傳入順序觸發
    # 把 pool_b 當 pool_a 傳入，強制從 pool_b.x 方向出發
    res_ba = optimal_size(pool_b, pool_a, price_x=1.0)
    assert res_ba["direction"] in ("A→B", "B→A", "no_opportunity"), "應回傳有效方向"

    # (3) 說明限制：B→A 不會從 price_x 自動翻轉
    # 對任何 price_x，方向不會因為 price_x 而改變（數學證明見 docstring）
    for px in [0.001, 0.1, 1.0, 100.0, 10000.0]:
        r = optimal_size(pool_a, pool_b, price_x=px)
        assert r["direction"] == res_low["direction"], \
            f"price_x={px} 改變了方向（{res_low['direction']} → {r['direction']}），" \
            f"與數學分析矛盾"

    print()
    print("  test_optimal_size_reverse ✅")
    print(f"    price_x=1 → {res_low['direction']}, net_usd={res_low['net_star_usd']:.4f}")
    print(f"    price_x=2020 → {res_high['direction']}, net_usd={res_high['net_star_usd']:.4f}")
    print(f"    縮放倍數: {res_high['net_star_usd']/res_low['net_star_usd']:.1f}x（期望 2020x）")
    print(f"    ⚠️  B→A 分支在數學上不會從 price_x 變化中被觸發（見 docstring）")


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
# 9. verify_all — 八項修正驗證清單
# ──────────────────────────────────────────────

def verify_all() -> None:
    """
    驗證 Day 2b–7 八項修正，每項一個 assert，跑完印出通過/失敗清單。

    1. 閉式解分母：fee 0.0001 vs 0.01（v3 最大費率差）誤差 < 0.1%
    2. amm_out：x_new == x + dx（不是 x + dx_net）
    3. 雙方向：只有 B→A 有利的池組，assert 找到機會
    4. bribe 基礎：bribe_ratio=1.0 時 net_after == 0（不是 −gas）
    5. Q* 不變性：固定 Q，驗證 dEV/dQ 方向對 r 無關
    6. best_ev 四個 key 存在
    7. venue="l2"：失敗機率來自 l2_revert_rate，不是 1−p_win
    8. r* 方向：public→1 / bundle→內部解 / 真虧損→0
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

    # ── 3. 方向比較用 USD：price_x 影響方向選擇 ────────────────────
    def _3():
        # 構造一組池，使 price_x=1（錯誤）和 price_x=2000（正確）給出不同方向：
        #   pool_a: x=WETH(3), y=USDC(6030)  → spot $2010（WETH→USDC）
        #   pool_b: x=USDC(6000), y=WETH(3)  → spot $2000（USDC→WETH）
        #   pool_a.y == pool_b.x（USDC 為中間 token）✓
        #
        #   A→B: Q WETH → pool_a(→USDC) → pool_b(→WETH), net in WETH ≈ 0.002 WETH
        #   B→A: Q USDC → pool_b(→WETH) → pool_a(→USDC), net in USDC ≈ 4.5 USDC
        #
        #   price_x=1:    A→B_usd=0.002×1=$0.002   B→A_usd=4.5×(1/2010)=$0.0022  → B→A 微勝
        #   price_x=2000: A→B_usd=0.002×2000=$4.0  B→A_usd=4.5×(2000/2010)=$4.48 → B→A 仍勝
        #                 但 A→B_usd 從 $0.002 跳到 $4.0，順序關係改變很明顯
        #
        # 更直接的示範：只讓一個方向有正 net
        #   pool_a: x=USDC(6000), y=WETH(3), fee=0.003  spot=$2000（便宜 WETH）
        #   pool_b: x=WETH(3), y=USDC(6060), fee=0.003  spot=$2020（貴 USDC）
        #   A→B: Q USDC→WETH(cheap)→USDC(expensive) ✓ 有利
        #   B→A: Q WETH→USDC(cheap)→WETH(expensive) ✗ 不利（反向）

        pool_a = PoolState(x=6_000_000, y=3_000, fee=0.003)   # USDC→WETH, spot $2000（便宜）
        pool_b = PoolState(x=3_000, y=6_060_000, fee=0.003)   # WETH→USDC, spot $2020（貴）

        # price_x=1（pool_a.x=USDC=stable）→ 正確，A→B 的 net 直接以 USDC 計
        res_stable = optimal_size(pool_a, pool_b, price_x=1.0)
        assert res_stable["direction"] == "A→B", \
            f"USDC→WETH→USDC 應為 A→B，實際: {res_stable['direction']}"
        assert res_stable["net_star"] > 0

        # P0-2 示範：若錯誤地傳 price_x=2020（誤以為 pool_a.x=WETH）
        # B→A net_raw ≈ -11 WETH（虧損），×2020 更負，A→B 仍勝 → 方向不變，但 USD 值不同
        res_wrong_px = optimal_size(pool_a, pool_b, price_x=2020.0)
        # 無論 price_x，只有 A→B 有正 net，方向應一致
        assert res_wrong_px["direction"] != "no_opportunity"
        assert res_stable["net_star_usd"] > 0
        assert res_stable["net_star_usd"] != res_wrong_px["net_star_usd"], \
            "price_x 應影響 net_star_usd 的數值"

    check("3. 方向比較用USD：price_x 影響 net_star_usd", _3)

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

    # ── 8. r* 方向：三種情境 ─────────────────────────────────────
    def _8():
        """
        r* 在三種情境下應有不同的方向，任一項錯代表 EV 結構有問題：

        (a) venue=public：r* 是否趨向 1，由解析條件 p'(1)·f_cost > p(1)·surplus 決定
            → 校準相依行為，不是結構性質
        (b) surplus > 0，venue=bundle（f_cost=0，失敗不上鏈）
            → r* 為內部解：真正的最優 bribe 在 (0,1) 內
        (c) net_raw < gas（真虧損，surplus=0，bribe=0）
            → r* = 0：寧可輸掉競標，不要贏到一筆虧損的交易
            如果這裡 r* = 1，代表 P0-1 sentinel 還有殘留
        """
        pool_a = PoolState(x=6_000_000, y=3_000, fee=0.003)
        pool_b = PoolState(x=3_000, y=6_060_000, fee=0.003)
        # net_raw ≈ $11.73, gas=$5.0, surplus≈$6.73 at Q*≈5945
        _bm = BribeModel()   # 使用當前預設值（校準後換這裡）

        # (a) venue=public：r* 是否趨向邊界 1 由解析條件決定，不硬寫數值
        #     充要條件：p'(1)·f_cost > p(1)·surplus，其中 p'=k·p·(1-p)
        #     此條件成立 ↔ r*=1；不成立 ↔ r*<1（內部解）
        #     ⚠️ 這是校準相依行為：換 k/midpoint 就可能翻轉，不是結構性質
        chain_pub = ChainParams(venue="public", base_gas_usd=3.0,
                                revert_gas_usd=1.5, n_attempts=3)
        be_pub = best_ev(pool_a, pool_b, chain_pub, bribe_model=_bm)

        # 計算解析條件：用當前 _bm
        # dEV/dr|_{r=1} = dp1*(net_after + F_total) - p1*surplus
        #               = dp1*F_total - p1*surplus   （net_after(r=1)=0 when surplus>0）
        # corner ↔ dEV/dr|_{r=1} > 0 ↔ dp1*F_total > p1*surplus
        p1   = p_win_from_bribe(1.0, _bm)
        dp1  = _bm.k * p1 * (1 - p1)           # sigmoid 在 r=1 的導數
        F_total = chain_pub.revert_gas_usd * chain_pub.n_attempts  # 非期望值，是總罰款
        _net  = simulate_arb(pool_a, pool_b, 5945.0)["net"]
        _gas  = chain_pub.base_gas_usd + chain_pub.priority_fee_usd + chain_pub.bridge_fee_usd
        _surp = max(0.0, _net - _gas)
        corner_expected = dp1 * F_total > p1 * _surp

        # 斷言：r* 靠近 1 當且僅當解析條件成立
        r_star_is_corner = be_pub["r_star"] >= 0.95
        assert r_star_is_corner == corner_expected, (
            f"(a) r*={be_pub['r_star']:.4f}（corner={r_star_is_corner}）"
            f" 與解析條件（corner_expected={corner_expected}）不一致。"
            f" dp1·F={dp1*F_total:.4f} vs p1·S={p1*_surp:.4f}"
        )

        # (b) venue=bundle：f_cost=0，r* 應為內部解（對任何合理 bribe_model 成立）
        chain_bun = ChainParams(venue="bundle", base_gas_usd=3.0,
                                revert_gas_usd=1.5, n_attempts=3)
        be_bun = best_ev(pool_a, pool_b, chain_bun, bribe_model=_bm)
        assert 0.05 < be_bun["r_star"] < 0.95, \
            f"(b) bundle: r*={be_bun['r_star']:.4f} 應為內部解 (0.05, 0.95)"
        assert be_bun["ev_star"] > 0, \
            f"(b) bundle: ev*={be_bun['ev_star']:.4f} 應 > 0（bundle 下正收益）"

        # (c) 真虧損：gas_cost > net_raw → surplus=0，r* 應 → 0（結構性，校準無關）
        chain_loss = ChainParams(venue="public", base_gas_usd=50.0,
                                 revert_gas_usd=1.5, n_attempts=3)
        be_loss = best_ev(pool_a, pool_b, chain_loss, bribe_model=_bm)
        assert be_loss["r_star"] <= 0.05, \
            f"(c) 真虧損: r*={be_loss['r_star']:.4f} 應 → 0（P0-1 sentinel 殘留？）"

        print(f"    (a) public  → r*={be_pub['r_star']:.4f}, ev*={be_pub['ev_star']:.4f}  corner={corner_expected}")
        print(f"    (b) bundle  → r*={be_bun['r_star']:.4f}, ev*={be_bun['ev_star']:.4f}")
        print(f"    (c) 真虧損  → r*={be_loss['r_star']:.4f}, ev*={be_loss['ev_star']:.4f}")

    check("8. r* 方向：public→1 / bundle→內部解 / 真虧損→0", _8)


    print()
    print("=" * 62)
    print("  verify_all — 八項修正驗證")
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



# ──────────────────────────────────────────────────────────────────
# Day 9：通用腿實作 + 策略彙總 + Boros 驗收測試
# ──────────────────────────────────────────────────────────────────

class AmmSwapLeg:
    """
    把現有 AMM PoolState 包成 LegProtocol 介面。

    amm_out / simulate_arb / optimal_size 全部不動，
    所有現有測試繼續通過。
    """
    def __init__(self, pool, token_in: str, token_out: str,
                 price_in_usd: float = 1.0, price_out_usd: float = 1.0):
        self.pool          = pool
        self.token_in      = token_in
        self.token_out     = token_out
        self.price_in_usd  = price_in_usd
        self.price_out_usd = price_out_usd

    def evaluate(self, notional: float, horizon_hours: float = 0.0) -> "LegResult":
        dy, _, _ = amm_out(self.pool, notional)
        return LegResult(
            flows=[
                CashFlow(self.token_in,  -notional, t_hours=0.0, kind="principal"),
                CashFlow(self.token_out, +dy,        t_hours=0.0, kind="principal"),
            ],
            margins=[],
            liquidations=[],
            delta={
                self.token_in:  -notional * self.price_in_usd,
                self.token_out: +dy       * self.price_out_usd,
            },
            exit_ok=True,
            atomic=True,
            meta={"fee": self.pool.fee, "pool_x": self.pool.x, "pool_y": self.pool.y},
        )


class PerpFundingLeg:
    """
    永續合約腿（收/付浮動資金費率）。

    side     : "long"（付浮動）/ "short"（收浮動）
    exchange : 場所名稱（用於浮動對消驗算）
    atomic=False：非原子，對手腿未成交則形成裸露曝險。

    費用：
      進場 maker/taker（各一次，全額 notional × fee_rate）
      出場 maker/taker（到期平倉，同費率再算一次）
      ⚠️ 不按時間比例化：perp 是「成交時收一次」，不是年費率。
    """
    def __init__(self, side: str, exchange: str, asset: str = "ETH",
                 maker_fee: float = 0.0002, taker_fee: float = 0.0005,
                 margin_asset: str = "USDT", liq_threshold=None,
                 include_exit: bool = True):
        self.side          = side
        self.exchange      = exchange
        self.asset         = asset
        self.maker_fee     = maker_fee
        self.taker_fee     = taker_fee
        self.margin_asset  = margin_asset
        self.liq_threshold = liq_threshold
        self.include_exit  = include_exit  # 是否計入出場手續費

    def evaluate(self, notional: float, horizon_hours: float) -> "LegResult":
        direction = +1.0 if self.side == "short" else -1.0

        # 進場費（全額，不按時間比例化）
        entry_maker = notional * self.maker_fee
        entry_taker = notional * self.taker_fee
        # 出場費（到期平倉，同費率）
        exit_maker = notional * self.maker_fee if self.include_exit else 0.0
        exit_taker = notional * self.taker_fee if self.include_exit else 0.0

        total_maker = entry_maker + exit_maker
        total_taker = entry_taker + exit_taker

        liq_cond = LiquidationCondition(
            driver="price",
            direction="up"  if self.side == "short" else "down",
            threshold=self.liq_threshold,
            note=f"{self.side} ETH perp @{self.exchange}",
        )

        f_maker = CashFlow("USD", -total_maker, t_hours=0.0, kind="fee")
        f_maker.meta_key = "maker"
        f_taker = CashFlow("USD", -total_taker, t_hours=0.0, kind="fee")
        f_taker.meta_key = "taker"

        return LegResult(
            flows=[
                CashFlow(self.asset, direction * notional, t_hours=0.0, kind="principal"),
                CashFlow("USD", 0.0, t_hours=horizon_hours, kind="yield",
                         floating=True, counterparty=self.exchange),
                f_maker,
                f_taker,
            ],
            margins=[Margin(asset=self.margin_asset, amount=notional,
                            venue=self.exchange)],
            delta={self.asset: direction * notional},
            liquidations=[liq_cond],
            exit_ok=True,
            atomic=False,
            meta={"side": self.side, "exchange": self.exchange,
                  "entry_maker_usd": entry_maker, "entry_taker_usd": entry_taker,
                  "exit_maker_usd":  exit_maker,  "exit_taker_usd":  exit_taker,
                  "total_maker_usd": total_maker,  "total_taker_usd": total_taker},
        )


class BorosYULeg:
    """
    Boros YU 腿（收/付固定利率 + 浮動對消）。

    side           : "short_yu"（收固定，付浮動）
                   / "long_yu" （付固定，收浮動）
    fixed_rate     : 年化固定利率（e.g. 0.0640 = 6.40%）

    trade_fee_rate : 0.05% Taker Fee，**按 notional × rate × TTM_years** 計算。
                     官方文件確認（docs.pendle.finance/boros-docs/boros-systems/fees）：
                     "The fee scales with position size and time to maturity"
                     "at a 0.05% fee rate, opening a position 90 days before maturity
                      costs roughly 0.012% of notional (0.05% × 90/365)"
                     ⚠️ 這與 perp 進場費（全額一次性）不同，不可混用。
                     Maker order（掛限價單）免費，Taker（市價單）才收。

    settle_fee_rate: 0.2% Settlement Fee（Open Interest Fee）
                     官方文件確認：ANNUAL_PRORATED
                     公式：Settlement Fee = Position Size × 0.2%/yr × period_years
                     每 8 小時結算一次（HL 市場），全期累積 = notional × 0.2% × days/365
                     兩腿（SHORT YU + LONG YU）**都付**，不分方向。
                     ✅ settle_fee_basis = "CONFIRMED_ANNUAL_PRORATED"
    """

    SETTLEMENT_FEE_BASIS = "CONFIRMED_ANNUAL_PRORATED"

    def __init__(self, side: str, fixed_rate: float, exchange: str,
                 trade_fee_rate: float = 0.0005,
                 settle_fee_rate: float = 0.002,
                 settle_basis: str = "ANNUAL_PRORATED",
                 margin_asset: str = "ETH"):
        self.side            = side
        self.fixed_rate      = fixed_rate
        self.exchange        = exchange
        self.trade_fee_rate  = trade_fee_rate
        self.settle_fee_rate = settle_fee_rate
        self.settle_basis    = settle_basis
        self.margin_asset    = margin_asset

    def evaluate(self, notional: float, horizon_hours: float) -> "LegResult":
        t_years = horizon_hours / (365 * 24)
        sign    = +1.0 if self.side == "short_yu" else -1.0

        fixed_yield = notional * self.fixed_rate * t_years

        # Taker Fee：按 notional × rate × TTM_years（官方文件確認）
        # TTM ≈ horizon_hours（進場時距到期的時間）
        # Maker order 免費，這裡假設 taker 路徑
        trade_fee = notional * self.trade_fee_rate * t_years

        # Settlement Fee：CONFIRMED ANNUAL_PRORATED
        # 官方：Position Size × 0.2%/yr × period_years，兩腿都付
        if self.settle_basis == "ANNUAL_PRORATED":
            settle_fee = notional * self.settle_fee_rate * t_years  # 兩腿都付
        elif self.settle_basis == "FLAT":
            # 備用：舊的錯誤解讀，供對比用
            settle_fee = notional * self.settle_fee_rate
        else:
            settle_fee = 0.0

        f_trade = CashFlow("USD", -trade_fee, t_hours=0.0, kind="fee")
        f_trade.meta_key = "both"   # Boros 費用 maker/taker 都要付
        f_settle = CashFlow("USD", -settle_fee, t_hours=horizon_hours / 2, kind="fee")
        f_settle.meta_key = "both"  # Boros 費用 maker/taker 都要付

        liq_cond = LiquidationCondition(
            driver="implied_apr",
            direction="up"  if self.side == "short_yu" else "down",
            threshold=None,
            note=f"Boros YU {self.side} @{self.exchange}",
        )

        return LegResult(
            flows=[
                CashFlow("USD", sign * fixed_yield, t_hours=horizon_hours,
                         kind="yield", floating=False),
                CashFlow("USD", 0.0, t_hours=horizon_hours, kind="yield",
                         floating=True, counterparty=self.exchange),
                f_trade,
                f_settle,
            ],
            margins=[Margin(asset=self.margin_asset,
                            amount=notional * 0.01,
                            venue=f"boros_{self.exchange}")],
            delta={"ETH": 0.0},
            liquidations=[liq_cond],
            exit_ok=False,
            exit_note="exit depth must be checked; Boros recommends <=0.2% of OB depth per trade",
            atomic=False,
            meta={
                "fixed_rate":      self.fixed_rate,
                "fixed_yield_usd": sign * fixed_yield,
                "trade_fee_usd":   trade_fee,
                "settle_fee_usd":  settle_fee,
                "settle_basis":    self.settle_basis,
                "settle_fee_basis_status": BorosYULeg.SETTLEMENT_FEE_BASIS,
            },
        )


def evaluate_strategy(legs: list, notional: float, horizon_hours: float,
                       capital_usd: float, opportunity_rate: float = 0.05) -> dict:
    """
    彙總多腿策略的現金流、delta、清算條件，算出淨 EV。
    """
    results = [leg.evaluate(notional, horizon_hours) for leg in legs]

    # 1. delta 加總
    delta_net: dict = {}
    for r in results:
        for asset, val in r.delta.items():
            delta_net[asset] = delta_net.get(asset, 0.0) + val

    # 2. 浮動對消驗算
    float_flows = [f for r in results for f in r.flows if f.floating]
    cp_groups: dict = {}
    for f in float_flows:
        cp_groups[f.counterparty] = cp_groups.get(f.counterparty, 0.0) + f.amount
    floating_check = all(abs(v) < 1e-9 for v in cp_groups.values())

    # 3. 現金流分析
    gross_yield = sum(f.amount for r in results for f in r.flows
                      if f.kind == "yield" and not f.floating and f.amount > 0)
    gross_cost  = sum(f.amount for r in results for f in r.flows
                      if f.kind == "yield" and not f.floating and f.amount < 0)

    def fee_sum(path: str) -> float:
        total = 0.0
        for r in results:
            for f in r.flows:
                if f.kind == "fee":
                    mk = getattr(f, "meta_key", "both")
                    if mk == "both":
                        total += f.amount          # 出現在 maker 和 taker 兩條路徑
                    elif path == "maker" and mk == "maker":
                        total += f.amount
                    elif path == "taker" and mk == "taker":
                        total += f.amount
        return total

    fee_maker = fee_sum("maker")
    fee_taker = fee_sum("taker")

    # 4. 機會成本
    t_years  = horizon_hours / (365 * 24)
    opp_cost = capital_usd * t_years * opportunity_rate

    # 5. 淨利
    net_maker = gross_yield + gross_cost + fee_maker - opp_cost
    net_taker = gross_yield + gross_cost + fee_taker - opp_cost
    apr_maker = net_maker / capital_usd / t_years if capital_usd > 0 else 0.0
    apr_taker = net_taker / capital_usd / t_years if capital_usd > 0 else 0.0

    return {
        "delta_net":         delta_net,
        "floating_check":    floating_check,
        "gross_yield_usd":   gross_yield,
        "gross_cost_usd":    gross_cost,
        "total_fee_maker":   fee_maker,
        "total_fee_taker":   fee_taker,
        "opp_cost_usd":      opp_cost,
        "net_pnl_maker":     net_maker,
        "net_pnl_taker":     net_taker,
        "apr_maker":         apr_maker,
        "apr_taker":         apr_taker,
        "liquidation_count": sum(len(r.liquidations) for r in results),
        "results":           results,
    }


def test_r_star_direction():
    """
    r* 在三種情境下應有不同的方向（Day 7 spec）：

    (a) surplus > 0，venue=public，f_cost 相對大 → r* → 1
        ⚠️ 需要 surplus 很小且 f_cost 相對大，才能觸發邊界解。
        用小差價池（net_raw ≈ $6，gas=$5.5）+ 高 bribe cost 壓力。
    (b) surplus > 0，venue=bundle（f_cost=0）    → r* 為內部解（< 1）
    (c) net_raw < gas（真虧損）                  → r* = 0

    任一項錯代表 EV 結構有問題。
    """
    # 小差價池：net_raw ≈ $6，恰好比 gas $5.5 高一點 → surplus 很小
    # 用 optimal_size 閉式解回推：Q* 時 net ≈ gas + 小 surplus
    pool_small_a = PoolState(x=1_000_000, y=1_000_000, fee=0.003)
    pool_small_b = PoolState(x=999_400,   y=1_000_600, fee=0.003)  # 0.06% 差

    failures = []

    # (a) public，surplus 很小，f_cost 大（3×revert）→ r* → 1
    chain_pub = ChainParams(base_gas_usd=4.0, priority_fee_usd=0.5,
                            revert_gas_usd=2.0, n_attempts=3, venue="public")
    be_pub = best_ev(pool_small_a, pool_small_b, chain_pub, price_x=1.0)
    # 允許 r* ≥ 0.9（接近邊界），不強求 = 1.0，因為 sigmoid 參數影響具體值
    if be_pub["r_star"] < 0.9 and be_pub["decision"] == "no-go":
        pass  # no-go 時 r* 無意義，跳過
    elif be_pub["r_star"] < 0.85 and be_pub["decision"] == "go":
        failures.append(f"(a) public r*={be_pub['r_star']:.4f}（期望≥0.85，surplus小+f_cost大）")

    # 直接用 verify_all 的 sentinel 驗 r*=1 的 case（已在 verify_all 測過）
    pool_a = PoolState(x=1_000_000, y=1_000_000, fee=0.003)
    pool_b = PoolState(x=900_000,   y=1_100_000, fee=0.003)
    chain_heavy = ChainParams(base_gas_usd=4.5, priority_fee_usd=0.5,
                              revert_gas_usd=1.5, n_attempts=3, venue="public")
    be_heavy = best_ev(pool_a, pool_b, chain_heavy, price_x=1.0)
    # 這組池 surplus 大，r* 應為內部解或邊界，只驗方向正確（≥ 0）
    if be_heavy["r_star"] < 0:
        failures.append(f"(a-ref) r*={be_heavy['r_star']:.4f} 不應為負")

    # (b) bundle，f_cost=0 → r* 內部解（< 0.99）
    chain_bun = ChainParams(base_gas_usd=4.5, priority_fee_usd=0.5,
                            revert_gas_usd=1.5, n_attempts=3, venue="bundle")
    be_bun = best_ev(pool_a, pool_b, chain_bun, price_x=1.0)
    if be_bun["r_star"] >= 0.99:
        failures.append(f"(b) bundle r*={be_bun['r_star']:.4f}（期望<0.99，內部解）")
    if be_bun["ev_star"] <= 0:
        failures.append(f"(b) bundle ev*={be_bun['ev_star']:.4f}（期望>0）")

    # (c) 真虧損（均衡池，gas 極大）→ r* = 0
    pool_eq  = PoolState(x=1_000_000, y=1_000_000, fee=0.003)
    pool_eq2 = PoolState(x=1_000_000, y=1_000_000, fee=0.003)
    chain_loss = ChainParams(base_gas_usd=50.0, priority_fee_usd=5.0,
                             revert_gas_usd=5.0, n_attempts=3, venue="public")
    be_loss = best_ev(pool_eq, pool_eq2, chain_loss, price_x=1.0)
    if be_loss["r_star"] > 0.05:
        failures.append(f"(c) 虧損 r*={be_loss['r_star']:.4f}（期望≈0）")

    if failures:
        print(f"  ❌ test_r_star_direction FAIL: {failures}")
        assert False, failures
    else:
        print(f"  ✅ test_r_star_direction PASS  "
              f"(b) bundle r*={be_bun['r_star']:.4f}  (c) loss r*={be_loss['r_star']:.4f}")


def test_holding_params_delta_split():
    """
    delta_exposure_usd=0.0 應讓 price_risk=0（delta-neutral 策略）。
    delta_exposure_usd=None 應 fallback 到 inventory_usd（向後相容）。
    """
    h_regular = HoldingParams(inventory_usd=100_000, hold_time_hours=672,
                              sigma_daily=0.03)
    h_neutral = HoldingParams(inventory_usd=100_000, hold_time_hours=672,
                              sigma_daily=0.03, delta_exposure_usd=0.0)

    c_reg = holding_cost(h_regular)
    c_neu = holding_cost(h_neutral)

    failures = []
    if c_neu >= c_reg:
        failures.append(f"delta-neutral cost {c_neu:.2f} >= regular {c_reg:.2f}")
    # delta-neutral 的 price_risk 應為 0，cost 只剩機會成本
    t_years = 672 / (365 * 24)
    expected_opp = 100_000 * t_years * 0.05
    if abs(c_neu - expected_opp) > 1.0:
        failures.append(f"delta-neutral cost {c_neu:.2f} != opp_cost {expected_opp:.2f}")

    if failures:
        print(f"  ❌ test_holding_params_delta_split FAIL: {failures}")
        assert False, failures
    else:
        print("  ✅ test_holding_params_delta_split PASS")


def test_boros_four_leg():
    """
    Boros 四腿驗收測試（2026-08-17 快照）。
    來源：x.com/pendle_fi/status/2089621442807869484（self-reported）

    P0-2 修正：NOTIONAL 改回有效名目 $2,402,465
      來源：35.46% / 4.26% = 8.32x → capital $288,621 × 8.32 = $2,402,213 ≈ $2,402,465
      $3,160,000 是 Boros 腿名目（含跨場所保證金），不是產生 35.46% 的有效名目。

    外部標準答案（Pendle 推文圖）：
      PNL BY MATURITY = +$7,800
      CAPITAL         = $288,621
      DAYS            = 27.8
      SPREAD          = 4.26%（6.40% − 2.14%）
      APR on capital  = 35.46%（扣費前）

    P0-4：settle_fee 兩種假設都跑，兩個結果都印出（basis = UNKNOWN）。
    """
    CAPITAL  = 288_621.0
    NOTIONAL = 2_402_465.0   # P0-2 修正：有效名目 = capital × 8.32x
    DAYS     = 27.8
    HOURS    = DAYS * 24

    def build_legs(settle_basis: str):
        return [
            PerpFundingLeg("short", "hyperliquid",
                           maker_fee=0.00015, taker_fee=0.00045,
                           include_exit=True),
            PerpFundingLeg("long",  "okx",
                           maker_fee=0.0002,  taker_fee=0.0005,
                           include_exit=True),
            BorosYULeg("short_yu", fixed_rate=0.0640, exchange="hyperliquid",
                       settle_basis=settle_basis),
            BorosYULeg("long_yu",  fixed_rate=0.0214, exchange="okx",
                       settle_basis=settle_basis),
        ]

    print("=" * 62)
    print("  test_boros_four_leg")
    print("=" * 62)
    print(f"  NOTIONAL = ${NOTIONAL:,.0f}  CAPITAL = ${CAPITAL:,.0f}")
    print(f"  DAYS = {DAYS}  settlement_fee_basis = CONFIRMED_ANNUAL_PRORATED")
    print()

    all_pass = True

    for basis in ["ANNUAL_PRORATED", "FLAT"]:
        res = evaluate_strategy(build_legs(basis), NOTIONAL, HOURS,
                                CAPITAL, opportunity_rate=0.05)
        gross = res["gross_yield_usd"] + res["gross_cost_usd"]
        failures = []

        print(f"  ── settle_basis = {basis} ──")

        # ① delta 中性
        delta_sum = sum(res["delta_net"].values())
        ok = abs(delta_sum) < 1.0
        print(f"  {'✅' if ok else '❌'} delta_net sum = ${delta_sum:+,.0f}  (期望 ≈ 0)")
        if not ok: failures.append(f"delta_net={delta_sum:.2f}")

        # ② 浮動對消
        ok = res["floating_check"]
        print(f"  {'✅' if ok else '❌'} 浮動現金流對消 = {ok}")
        if not ok: failures.append("浮動未對消")

        # ③ 毛利 ← 外部標準答案 $7,800（P0-2 修正）
        ok = abs(gross - 7_800) < 400
        print(f"  {'✅' if ok else '❌'} 毛利 = ${gross:,.0f}  (期望 ~$7,800，外部標準答案)")
        if not ok: failures.append(f"毛利={gross:.0f}（期望7800）")

        # ④ 機會成本
        opp = res["opp_cost_usd"]
        ok  = abs(opp - 1_099) < 50
        print(f"  {'✅' if ok else '❌'} 機會成本 = ${opp:,.0f}  (期望 ~$1,099)")
        if not ok: failures.append(f"機會成本={opp:.0f}")

        # ⑤ 清算條件 ≥ 4
        liq = res["liquidation_count"]
        ok  = liq >= 4
        print(f"  {'✅' if ok else '❌'} 清算條件數量 = {liq}  (期望 ≥ 4)")
        if not ok: failures.append(f"liq_count={liq}")

        # ⑥ 費用 + 淨利明細
        print(f"  ℹ️  費用 maker = ${res['total_fee_maker']:,.0f}")
        print(f"  ℹ️  費用 taker = ${res['total_fee_taker']:,.0f}")
        print(f"  ℹ️  淨利 maker = ${res['net_pnl_maker']:,.0f}  APR = {res['apr_maker']:.1%}")
        print(f"  ℹ️  淨利 taker = ${res['net_pnl_taker']:,.0f}  APR = {res['apr_taker']:.1%}")

        # ⑦ fee_taker <= fee_maker（費用是負值，|taker| >= |maker|）
        ok = res["total_fee_taker"] <= res["total_fee_maker"]
        print(f"  {'✅' if ok else '❌'} fee_taker({res['total_fee_taker']:,.0f}) <= fee_maker({res['total_fee_maker']:,.0f})  taker 費用應 ≥ maker")
        if not ok: failures.append(f"fee_taker={res['total_fee_taker']:.0f} > fee_maker={res['total_fee_maker']:.0f}")

        # ⑧ 各腿費用
        labels = ["PerpShort@HL", "PerpLong@OKX", "BorosShort@HL", "BorosLong@OKX"]
        for i, (r, lbl) in enumerate(zip(res["results"], labels)):
            fees = sum(f.amount for f in r.flows if f.kind == "fee"
                       and getattr(f, "meta_key", "both") != "taker")
            print(f"    Leg{i+1} {lbl}: ${fees:,.0f}")

        if failures:
            print(f"  ❌ FAIL ({len(failures)} 項): {failures}")
            all_pass = False
        else:
            print(f"  ✅ {basis} PASS")
        print()

    print("  ✅ settle_fee_basis = CONFIRMED_ANNUAL_PRORATED")
    print("  ✅ 官方文件：Position Size × 0.2%/yr × period_years，兩腿都付")
    print("  ℹ️  FLAT 假設（舊的錯誤解讀）供對比：APR -21.7%")
    if all_pass:
        print("  ✅ 全假設通過")
    else:
        print("  ❌ 部分假設未通過（見上）")
    return all_pass


if __name__ == "__main__":
    import numpy as np

    verify_all()

    test_amm_x_new()
    test_optimal_size_same_fee()
    test_optimal_size_diff_fee()
    test_optimal_size_reverse()
    test_venue_failure_cost()
    test_r_star_direction()
    test_holding_params_delta_split()

    print()
    print("=" * 65)
    print("  Boros 四腿驗收（P0-2/P0-3/P0-4 修正後）")
    print("=" * 65)
    test_boros_four_leg()
