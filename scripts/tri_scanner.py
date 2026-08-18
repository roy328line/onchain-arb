"""
tri_scanner.py — 三角套利掃描器（Day 5：加入 Uniswap v3）

從 Tycho 串流接收所有池狀態（v2 + v3），建構 token graph，
枚舉所有三角路徑（A→B→C→A），用 optimal_size_tri() 掃描機會。

核心邏輯：
  1. PoolRegistry：追蹤所有池的 token pair + reserve
  2. TokenGraph：每個 token → 鄰接池列表
  3. 枚舉三角路徑：對每個起始 token A，
     找所有 (pool_AB, pool_BC, pool_CA) 組合
  4. 對每條路徑呼叫 optimal_size_tri()，過濾 net_star > GAS_COST

v3 虛擬儲備推算（Day 5 新增）：
  Uniswap v3 在 current tick 附近可近似為 x·y=k，虛擬儲備由以下推導：
    sqrtP = sqrt_price_x96 / 2^96
    virtual_r0 = liquidity / sqrtP    （token0 raw units）
    virtual_r1 = liquidity * sqrtP    （token1 raw units）
  此近似在小規模交易（Q << pool depth）下誤差 < 1%，足夠快篩用途。

v3 fee tier（pips，/1,000,000）：
  500  → 0.05%（5 bps）
  3000 → 0.30%（30 bps）
  10000→ 1.00%（100 bps）
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from itertools import permutations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from models.ev_model import PoolState, ChainParams, Leg, simulate_tri_arb, optimal_size_tri

# ── DuckDB 記錄（Day 13）──────────────────────────────────────
_DB_PATH = Path(__file__).parent.parent / "data" / "arb.duckdb"

def _db_insert_results(results: list[dict], block: int) -> None:
    """把 Quoter 驗證後的掃描結果寫入 scan_results 表（含 OBSERVE no-go）。"""
    if not results:
        return
    import duckdb
    con = duckdb.connect(str(_DB_PATH))
    for r in results:
        try:
            con.execute("""
                INSERT INTO scan_results
                  (block, path, token_a, token_b, token_c,
                   dex_ab, dex_bc, dex_ca, Q_star,
                   net_star_usd, net_real_usd, source, go_real, gas_cost)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, [
                block,
                f"{r['token_a'][:8]}→{r['token_b'][:8]}→{r['token_c'][:8]}",
                r["token_a"], r["token_b"], r["token_c"],
                r.get("dex_ab",""), r.get("dex_bc",""), r.get("dex_ca",""),
                r.get("Q_star", 0),
                r.get("net_star_usd", 0),
                r.get("net_real_usd", None),
                r.get("source", "unknown"),
                bool(r.get("go_real", False)),
                GAS_COST,
            ])
        except Exception as e:
            print(f"[DB WARN] insert failed: {e}", flush=True)
    con.close()


KEY       = os.environ.get("TYCHO_API_KEY", "")
TYCHO_URL = "tycho-fynd-ethereum.propellerheads.xyz:443"
BINARY    = Path(__file__).parent / "tycho-client"

GAS_COST     = 5.5   # $5.5 per tx（三腿比兩腿 gas 略高，之後可調整）
# Day 9：三角掃描器也切換到 bundle venue（f_cost=0）
# 統一與 tycho_scanner 的 venue 設定
CHAIN_BUNDLE = ChainParams(venue="bundle", base_gas_usd=4.0, priority_fee_usd=1.5)
SCAN_SECONDS = int(os.environ.get("SCAN_SECONDS", "300"))
MIN_TVL      = 10
MIN_POOL_TVL = int(os.environ.get("MIN_POOL_TVL", "50000"))  # 每腿最低 TVL（USD）

# 已知 token 價格（用於估算 TVL）
TOKEN_PRICES = {
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": 1900,   # WETH
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": 1,      # USDC
    "0x6b175474e89094c44da98b954eedeac495271d0f": 1,      # DAI
    "0xdac17f958d2ee523a2206206994597c13d831ec7": 1,      # USDT
    "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599": 65000,  # WBTC
    "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984": 7,      # UNI
    "0xe76c6c83af64e4c60245d8c7de953df673a7a33d": 1,      # RAIL（近似 $1）
}

# 已知 decimals（避免 RPC 查詢）
TOKEN_DECIMALS = {
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": 6,   # USDC
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": 18,  # WETH
    "0x6b175474e89094c44da98b954eedeac495271d0f": 18,  # DAI
    "0xdac17f958d2ee523a2206206994597c13d831ec7": 6,   # USDT
    "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599": 8,   # WBTC
    "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984": 18,  # UNI
}

# 以 USDC 計價的近似匯率（用於把 net 換算成 USD）
# 三角套利的 net 是用起始 token 計價的，需要換算
TOKEN_USD_PRICE = {
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": 1.0,      # USDC
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": 1900.0,   # WETH
    "0x6b175474e89094c44da98b954eedeac495271d0f": 1.0,      # DAI
    "0xdac17f958d2ee523a2206206994597c13d831ec7": 1.0,      # USDT
    "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599": 65000.0,  # WBTC
    "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984": 7.0,      # UNI
}


def hex_to_int(val) -> int | None:
    if val is None:
        return None
    if isinstance(val, int):
        return val
    s = str(val)
    if s.startswith(("0x", "0X")):
        return int(s, 16)
    try:
        return int(s)
    except ValueError:
        return None


class PoolRegistry:
    """追蹤所有池的狀態（不限 token pair）。"""

    def __init__(self):
        # addr → { t0, t1, d0, d1, fee, dex, r0, r1 }
        self.pools: dict[str, dict] = {}
        self.meta:  dict[str, dict] = {}

    def register_component(self, addr: str, comp: dict) -> bool:
        tokens = comp.get("tokens", [])
        if len(tokens) < 2:
            return False
        t0 = tokens[0].lower()
        t1 = tokens[1].lower()

        dex = comp.get("protocol_system", "?")
        sa  = comp.get("static_attributes", {})

        # ── fee 換算：v3 用 pips（/1,000,000），v2 用 bps（/10,000）──
        if "v3" in dex:
            # v3 fee：500 → 0.05%，3000 → 0.3%，10000 → 1%
            fee_int = hex_to_int(sa.get("fee", "0xbb8")) or 3000  # 0xbb8=3000
            fee = fee_int / 1_000_000
            is_v3 = True
        else:
            # v2 fee：30 → 0.3%（0x1e = 30）
            fee_int = hex_to_int(sa.get("fee", "0x1e")) or 30
            fee = fee_int / 10_000
            is_v3 = False

        self.meta[addr] = {
            "t0": t0, "t1": t1,
            "d0": TOKEN_DECIMALS.get(t0, 18),
            "d1": TOKEN_DECIMALS.get(t1, 18),
            "fee": fee,
            "dex": dex,
            "is_v3": is_v3,
        }
        return True
    def update_state(self, addr: str, attrs: dict) -> bool:
        """用 reserve0/reserve1 或 v3 虛擬儲備更新池狀態。"""
        meta = self.meta.get(addr)
        if meta is None:
            return False

        tick_aware = True   # v2 池不適用 tick 問題，預設 True

        # ── v2 路徑：直接讀 reserve0 / reserve1 ──
        r0_raw = attrs.get("reserve0") or attrs.get("Reserve0")
        r1_raw = attrs.get("reserve1") or attrs.get("Reserve1")

        if r0_raw is not None and r1_raw is not None:
            r0_int = hex_to_int(r0_raw)
            r1_int = hex_to_int(r1_raw)
            if r0_int is None or r1_int is None:
                return False
            r0 = r0_int / 10 ** meta["d0"]
            r1 = r1_int / 10 ** meta["d1"]

        else:
            # ── v3 路徑：從 sqrt_price_x96 + liquidity + tick range 推算虛擬儲備 ──
            sqrt_raw = (attrs.get("sqrt_price_x96")
                        or attrs.get("sqrtPriceX96")
                        or attrs.get("sqrt_price"))
            liq_raw  = attrs.get("liquidity")
            tick_raw = attrs.get("tick")

            if sqrt_raw is None or liq_raw is None:
                return False

            sqrt_int = hex_to_int(sqrt_raw)
            liq_int  = hex_to_int(liq_raw)

            if not sqrt_int or not liq_int:
                return False

            Q96   = 2 ** 96
            sqrtP = sqrt_int / Q96
            L     = liq_int

            # 解析 current tick（Tycho hex 24-bit 有號整數）
            current_tick = None
            if tick_raw is not None:
                t_int = hex_to_int(tick_raw)
                if t_int is not None:
                    current_tick = t_int - (2 ** 24) if t_int >= 2 ** 23 else t_int

            # 收集所有初始化 tick
            init_ticks = []
            for k in attrs:
                if k.startswith("ticks/") and k.endswith("/net-liquidity"):
                    try:
                        init_ticks.append(int(k.split("/")[1]))
                    except ValueError:
                        pass

            # tick-aware 公式
            # r0 = L*(sqrtP_upper - sqrtP)/(sqrtP*sqrtP_upper)
            # r1 = L*(sqrtP - sqrtP_lower)
            tick_aware = False
            if current_tick is not None and init_ticks:
                below = [t for t in init_ticks if t <= current_tick]
                above = [t for t in init_ticks if t > current_tick]
                if below and above:
                    tl = max(below)
                    tu = min(above)
                    try:
                        import math as _math
                        sqrtP_l = _math.sqrt(1.0001 ** tl)
                        sqrtP_u = _math.sqrt(1.0001 ** tu)
                        if sqrtP_l < sqrtP < sqrtP_u and sqrtP * sqrtP_u > 0:
                            r0 = L * (sqrtP_u - sqrtP) / (sqrtP * sqrtP_u) / 10 ** meta["d0"]
                            r1 = L * (sqrtP - sqrtP_l) / 10 ** meta["d1"]
                            tick_aware = True
                    except (OverflowError, ZeroDivisionError):
                        pass

            if not tick_aware:
                # 退化：舊公式（忽略 tick range）
                # ⚠️ 接近 tick 邊界時高估數億倍（RAIL/USDT 坑，Day 6）
                r0 = (L / sqrtP) / 10 ** meta["d0"]
                r1 = (L * sqrtP) / 10 ** meta["d1"]

        if r0 <= 0 or r1 <= 0:
            return False

        # Day 9：虛擬儲備比率過濾
        # 當 sqrtP 接近 tick 邊界時，r0/r1 會爆炸（例如 r0=155T, r1=6）
        # 真實可交易池的儲備比不會超過 ~10,000x（即使 BTC/SHIB 也不到 1000x）
        # 超過此比率的池標記為 tick_aware=False（隔離到 OBSERVE）
        MAX_RESERVE_RATIO = 10_000
        if r0 > 0 and r1 > 0:
            ratio = max(r0, r1) / min(r0, r1)
            if ratio > MAX_RESERVE_RATIO:
                tick_aware = False  # 儲備比率異常 → 流動性幻覺

        # Day 9：implied price 過濾（v3 tick-aware 公式仍可能產生 5:1 的穩定幣比率）
        # 若兩個 token 都在 TOKEN_USD_PRICE 內，用已知價格算 implied_usd_ratio
        # implied_usd_ratio = (r1 * p1) / (r0 * p0) ≈ 1.0（均衡時）
        # 穩定幣對（p0≈p1≈1）：允許範圍 [0.5, 2.0]（5.24x 的 DAI/USDC 必須過濾掉）
        # 一般 token 對（e.g. WETH/USDC）：允許範圍 [0.05, 20.0]（流動性深度可以偏）
        if tick_aware and r0 > 0 and r1 > 0:
            p0 = TOKEN_USD_PRICE.get(meta["t0"], 0)
            p1 = TOKEN_USD_PRICE.get(meta["t1"], 0)
            if p0 > 0 and p1 > 0:
                implied_ratio = (r1 * p1) / (r0 * p0)
                # 穩定幣對：p0 ≈ p1 ≈ 1，用更嚴格的比率限制
                both_stable = (0.9 <= p0 <= 1.1) and (0.9 <= p1 <= 1.1)
                max_ratio = 2.0 if both_stable else 5.0
                min_ratio = 1.0 / max_ratio
                if not (min_ratio <= implied_ratio <= max_ratio):
                    tick_aware = False  # implied price 異常 → 流動性幻覺

        # v2 池預設 tick_aware=True（不適用 tick 問題）
        self.pools[addr] = {**meta, "r0": r0, "r1": r1, "tick_aware": tick_aware}
        return True

    def process_message(self, msg: dict) -> int:
        updated = 0
        for dex, dm in msg.get("state_msgs", {}).items():
            snaps = dm.get("snapshots", {})
            for addr, entry in snaps.get("states", {}).items():
                addr = addr.lower()
                comp = entry.get("component", {})
                if comp:
                    self.register_component(addr, comp)
                attrs = entry.get("state", {}).get("attributes", {})
                if self.update_state(addr, attrs):
                    updated += 1
            deltas = dm.get("deltas", {})
            for addr, upd in deltas.get("state_updates", {}).items():
                if not isinstance(upd, dict):
                    continue
                addr = addr.lower()
                attrs = upd.get("updated_attributes", {})
                if self.update_state(addr, attrs):
                    updated += 1
        return updated


class TokenGraph:
    """
    token → 鄰接池列表。
    用於快速找到三角路徑：
      A → [pool_AB, pool_AC, ...] → B → [pool_BC, ...] → C → [pool_CA]
    """

    def __init__(self):
        # token → list of (addr, other_token)
        self.edges: dict[str, list[tuple[str, str]]] = defaultdict(list)

    def rebuild(self, pools: dict):
        self.edges.clear()
        for addr, p in pools.items():
            self.edges[p["t0"]].append((addr, p["t1"]))
            self.edges[p["t1"]].append((addr, p["t0"]))

    def find_triangles(self, token_a: str) -> list[tuple[str, str, str, str, str]]:
        """
        找所有以 token_a 為起點的三角路徑。
        回傳 list of (addr_ab, token_b, addr_bc, token_c, addr_ca)
        """
        triangles = []
        for addr_ab, token_b in self.edges.get(token_a, []):
            if token_b == token_a:
                continue
            for addr_bc, token_c in self.edges.get(token_b, []):
                if token_c == token_a or token_c == token_b:
                    continue
                # 找 C→A 的池
                for addr_ca, token_back in self.edges.get(token_c, []):
                    if token_back == token_a and addr_ca != addr_ab and addr_ca != addr_bc:
                        triangles.append((addr_ab, token_b, addr_bc, token_c, addr_ca))
        return triangles


def pool_as_state(pool: dict, input_token: str) -> PoolState:
    """把 registry pool dict 轉成 PoolState（輸入 token 為 x）。"""
    if pool["t0"] == input_token:
        return PoolState(x=pool["r0"], y=pool["r1"], fee=pool["fee"])
    else:
        return PoolState(x=pool["r1"], y=pool["r0"], fee=pool["fee"])


# ── Day 11：鏈上報價工具 ────────────────────────────────────────

_ETH_RPC = "https://ethereum.publicnode.com"
_RPC_HEADERS = {"User-Agent": "curl/7.88.1", "Content-Type": "application/json"}
_ROUTER_V2 = "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D"
_QUOTER_V3 = "0xb27308f9F90D607463bb33eA1BeBb41C27CE5AB6"


def _eth_call(to: str, data: str) -> str:
    """發送 eth_call，回傳 hex 字串。失敗拋 RuntimeError。"""
    import urllib.request as _ur
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "eth_call",
        "params": [{"to": to, "data": data}, "latest"]
    }).encode()
    req  = _ur.Request(_ETH_RPC, data=body, headers=_RPC_HEADERS)
    resp = json.loads(_ur.urlopen(req, timeout=8).read())
    if "error" in resp:
        raise RuntimeError(f"eth_call error: {resp['error']}")
    return resp["result"]


def _p32(v: int) -> str:
    return format(int(v), "064x")


def _pa(addr: str) -> str:
    return _p32(int(addr, 16))


def v2_quote_onchain(path_addrs: list[str], amount_in_wei: int) -> list[int]:
    """
    呼叫 UniswapV2Router02.getAmountsOut(amountIn, path)。
    回傳每步的 amount（wei），長度 = len(path)。

    Day 10 驗證：v2 amm_out 誤差 < 0.4%，但 Quoter 是權威來源。
    """
    n    = len(path_addrs)
    cd   = ("0xd06ca61f"
            + _p32(amount_in_wei)
            + _p32(0x40)
            + _p32(n)
            + "".join(_pa(a) for a in path_addrs))
    res  = _eth_call(_ROUTER_V2, cd)
    raw  = bytes.fromhex(res.removeprefix("0x"))
    off  = int.from_bytes(raw[:32], "big")
    cnt  = int.from_bytes(raw[off:off+32], "big")
    return [
        int.from_bytes(raw[off+32 + i*32 : off+64 + i*32], "big")
        for i in range(cnt)
    ]


def v3_quote_onchain(token_in: str, token_out: str, fee: int,
                     amount_in_wei: int) -> int:
    """
    呼叫 UniswapV3Quoter.quoteExactInputSingle。
    回傳 amount_out（wei）。

    Day 10 驗證：v3 虛擬儲備高估 7.6%，Quoter 是唯一可信來源。
    fee 單位：pips（500=0.05%, 3000=0.3%, 10000=1%）。
    """
    cd = ("0xf7729d43"
          + _pa(token_in)
          + _pa(token_out)
          + _p32(fee)
          + _p32(amount_in_wei)
          + _p32(0))           # sqrtPriceLimitX96 = 0（無限制）
    res = _eth_call(_QUOTER_V3, cd)
    return int(res, 16)


def verify_onchain(opp: dict, reg: dict) -> dict | None:
    """
    對一條候選機會做鏈上 Quoter 二次驗證。

    流程：
      對每條腿依 dex 類型選擇報價來源：
        v2 腿 → v2_quote_onchain（單腿 getAmountsOut）
        v3 腿 → v3_quote_onchain（quoteExactInputSingle）

    回傳含 net_real_usd / go_real 的 dict；若 eth_call 失敗回傳 None。

    注意：這裡用逐腿串接（非 Router 一次打全路徑），因為三腿路徑跨 DEX，
    Router 無法處理。每腿獨立 eth_call，共 1-3 次。
    """
    legs = opp.get("legs", [])
    if not legs:
        return None

    try:
        token_a = opp["token_a"]
        price_a = TOKEN_USD_PRICE.get(token_a, 1.0)

        amount = int(opp["Q_star"] * (10 ** TOKEN_DECIMALS.get(token_a, 18)))

        for leg in legs:
            d0 = TOKEN_DECIMALS.get(leg.token_in,  18)
            d1 = TOKEN_DECIMALS.get(leg.token_out, 18)
            dex = leg.dex.lower()

            if "v3" in dex or "uniswap_v3" in dex:
                # v3：用 Quoter
                fee_pips = int(round(leg.fee * 1_000_000))
                amount = v3_quote_onchain(leg.token_in, leg.token_out,
                                          fee_pips, amount)
            else:
                # v2：用 getAmountsOut（單腿）
                outs   = v2_quote_onchain([leg.token_in, leg.token_out], amount)
                amount = outs[1]

        # 轉回 token 單位
        d_a        = TOKEN_DECIMALS.get(token_a, 18)
        q_out      = amount / (10 ** d_a)
        q_in       = opp["Q_star"]
        net_real   = q_out - q_in
        net_real_usd = net_real * price_a

        return {
            **opp,
            "net_real_usd": round(net_real_usd, 4),
            "go_real":      net_real_usd > GAS_COST,
            "verified":     True,
        }
    except Exception as e:
        return {**opp, "verify_error": str(e), "verified": False}


def estimate_pool_tvl(pool: dict) -> float:
    """保守估算池 TVL：只用已知 token 那側儲備 × 2。"""
    t0, t1 = pool["t0"], pool["t1"]
    r0, r1 = pool["r0"], pool["r1"]
    p0 = TOKEN_PRICES.get(t0, 0)
    p1 = TOKEN_PRICES.get(t1, 0)
    if p0 > 0 and p1 > 0:
        return r0 * p0 + r1 * p1
    elif p0 > 0:
        return r0 * p0 * 2
    elif p1 > 0:
        return r1 * p1 * 2
    return 0.0


def scan_triangles(reg: PoolRegistry, graph: TokenGraph, gas_cost: float,
                   min_pool_tvl: float = MIN_POOL_TVL) -> list[dict]:
    """
    掃描所有三角路徑，回傳 net_star_usd > gas_cost 的機會。

    過濾條件（排除流動性幻覺）：
      - 三條腿的 token 都必須是已知 token（TOKEN_PRICES 內）
      - 每條腿的 TVL >= min_pool_tvl（預設 $50,000）
    """
    results = []
    checked = set()

    for token_a in list(graph.edges.keys()):
        # 只掃已知 token 作為起點
        if token_a not in TOKEN_PRICES:
            continue

        triangles = graph.find_triangles(token_a)
        for (addr_ab, token_b, addr_bc, token_c, addr_ca) in triangles:
            # 三個 token 都必須是已知 token
            if token_b not in TOKEN_PRICES or token_c not in TOKEN_PRICES:
                continue

            # dedup：旋轉正規化（把最小位址轉到開頭），但保留順序以區分方向
            # A→B→C→A 和 A→C→B→A 是不同的套利方向，必須各自保留
            tri_addrs = [addr_ab, addr_bc, addr_ca]
            min_idx = tri_addrs.index(min(tri_addrs))
            key = tuple(tri_addrs[min_idx:] + tri_addrs[:min_idx])
            if key in checked:
                continue
            checked.add(key)

            p_ab = reg.pools.get(addr_ab)
            p_bc = reg.pools.get(addr_bc)
            p_ca = reg.pools.get(addr_ca)
            if not (p_ab and p_bc and p_ca):
                continue

            # ── TVL 過濾（排除流動性幻覺）──
            if min_pool_tvl > 0:
                tvl_ab = estimate_pool_tvl(p_ab)
                tvl_bc = estimate_pool_tvl(p_bc)
                tvl_ca = estimate_pool_tvl(p_ca)
                if min(tvl_ab, tvl_bc, tvl_ca) < min_pool_tvl:
                    continue

            try:
                pool_ab = pool_as_state(p_ab, token_a)
                pool_bc = pool_as_state(p_bc, token_b)
                pool_ca = pool_as_state(p_ca, token_c)

                opt = optimal_size_tri(pool_ab, pool_bc, pool_ca)
                if opt["direction"] == "no_opportunity":
                    continue

                net_star = opt["net_star"]
                Q_star   = opt["Q_star"]
                price_a  = TOKEN_USD_PRICE.get(token_a, 1.0)
                net_star_usd = net_star * price_a

                # tick_aware：三條腿都必須 True，任一 False 進 OBSERVE
                all_tick_aware = (
                    p_ab.get("tick_aware", True) and
                    p_bc.get("tick_aware", True) and
                    p_ca.get("tick_aware", True)
                )

                # Day 13：NEAR_MISS_THRESHOLD — net_star > $1 就建 entry（含 legs）
                # 讓 Quoter 可以驗「接近正 EV」的候選，不限於 > gas_cost 的路徑
                NEAR_MISS_THRESHOLD = 1.0
                if net_star_usd > NEAR_MISS_THRESHOLD:
                    # 用最優規模重跑一次取中間量 W, V（組裝 Leg 用）
                    sim = simulate_tri_arb(pool_ab, pool_bc, pool_ca, Q_star)
                    W   = sim["W"]    # token_b 量
                    V   = sim["V"]    # token_c 量
                    Q_out = sim["Q_out"]

                    # Day 9：腿深度過濾（防止耗盡淺池產生幻覺）
                    # 每腿的輸入不得超過該池 r0（同單位輸入端儲備）的 30%
                    # 超過代表模型在假設「可以把池子榨乾」，現實不可能發生
                    MAX_DEPTH = 0.30
                    ab_ok = (Q_star <= pool_ab.x * MAX_DEPTH)
                    bc_ok = (W      <= pool_bc.x * MAX_DEPTH)
                    ca_ok = (V      <= pool_ca.x * MAX_DEPTH)
                    depth_ok = ab_ok and bc_ok and ca_ok

                    if not depth_ok:
                        # 標記為 tick_aware=False → 隔離到 OBSERVE
                        all_tick_aware = False

                    legs = [
                        Leg(pool_addr=addr_ab, token_in=token_a, token_out=token_b,
                            amount_in=Q_star, amount_out=W,
                            dex=p_ab["dex"], fee=pool_ab.fee),
                        Leg(pool_addr=addr_bc, token_in=token_b, token_out=token_c,
                            amount_in=W,      amount_out=V,
                            dex=p_bc["dex"], fee=pool_bc.fee),
                        Leg(pool_addr=addr_ca, token_in=token_c, token_out=token_a,
                            amount_in=V,      amount_out=Q_out,
                            dex=p_ca["dex"], fee=pool_ca.fee),
                    ]

                    # is_candidate：net_star_usd 超過 gas 門檻（真正的「可執行」候選）
                    is_candidate = net_star_usd > gas_cost

                    entry = {
                        "token_a": token_a,
                        "token_b": token_b,
                        "token_c": token_c,
                        "addr_ab": addr_ab,
                        "addr_bc": addr_bc,
                        "addr_ca": addr_ca,
                        "dex_ab":  p_ab["dex"],
                        "dex_bc":  p_bc["dex"],
                        "dex_ca":  p_ca["dex"],
                        "Q_star":  Q_star,
                        "net_star_token": net_star,
                        "net_star_usd":   round(net_star_usd, 4),
                        "tvl_min": round(min(estimate_pool_tvl(p_ab),
                                            estimate_pool_tvl(p_bc),
                                            estimate_pool_tvl(p_ca)), 0),
                        "tick_aware": all_tick_aware,
                        "is_candidate": is_candidate,  # Day 13：區分真候選 vs 近似機會
                        "legs": legs,   # Day 9：Leg 抽象，組裝 bundle tx 用
                    }
                    results.append(entry)
            except Exception:
                continue

    # 只回傳 tick_aware=True 的；False 的隔離到 observe
    observe = [r for r in results if not r["tick_aware"]]
    results = [r for r in results if r["tick_aware"]]
    results.sort(key=lambda x: x["net_star_usd"], reverse=True)
    observe.sort(key=lambda x: x["net_star_usd"], reverse=True)

    # Day 11：第二階段 Quoter 驗證
    # 對候選裡含 v3 腿的路徑打 eth_call，修正 net_real_usd
    # v2-only 路徑直接標記 verified=True（amm_out 誤差 < 0.4%）
    verified = []
    for opp in results:
        has_v3 = any("v3" in leg.dex.lower() for leg in opp.get("legs", []))
        if not has_v3:
            verified.append({**opp, "net_real_usd": opp["net_star_usd"],
                              "go_real": opp.get("is_candidate", True),
                              "verified": True, "source": "v2_model"})
        else:
            v = verify_onchain(opp, {})
            if v:
                v["source"] = "quoter"
                v["go_real"] = v["net_real_usd"] > gas_cost
                verified.append(v)

    # Day 13：OBSERVE top 10 也跑 Quoter，記錄 net_real（找最接近正 EV 的時刻）
    obs_verified = []
    for opp in observe[:10]:
        has_v3 = any("v3" in leg.dex.lower() for leg in opp.get("legs", []))
        if not has_v3:
            obs_verified.append({**opp, "net_real_usd": opp["net_star_usd"],
                                  "go_real": False, "verified": True, "source": "v2_obs"})
        else:
            v = verify_onchain(opp, {})
            if v:
                v["source"] = "quoter_obs"
                v["go_real"] = False  # OBSERVE 永不執行，只記錄
                obs_verified.append(v)

    # Day 13：所有 verified 結果都回傳（不管 go_real），讓主迴圈記錄到 DuckDB
    # confirmed = go_real=True（真正輸出），all_verified = 含 no-go + obs（記錄用）
    confirmed    = [v for v in verified if v.get("go_real")]
    all_verified = verified + obs_verified   # 含 no-go + observe，供 DuckDB 記錄
    confirmed.sort(key=lambda x: x["net_real_usd"], reverse=True)
    return confirmed, observe, all_verified



TOKEN_SHORT = {
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": "USDC",
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": "WETH",
    "0x6b175474e89094c44da98b954eedeac495271d0f": "DAI",
    "0xdac17f958d2ee523a2206206994597c13d831ec7": "USDT",
    "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599": "WBTC",
    "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984": "UNI",
}

def short(addr: str) -> str:
    return TOKEN_SHORT.get(addr, addr[:6] + "…")


def scan(scan_seconds: int = SCAN_SECONDS):
    if not KEY:
        print("❌ TYCHO_API_KEY 未設定", file=sys.stderr)
        sys.exit(1)

    print("🔺 三角套利 Scanner 啟動")
    print(f"   掃描時長 : {scan_seconds}s")
    print(f"   Gas 門檻 : net_star_usd > ${GAS_COST:.2f}")
    print()

    cmd = [
        str(BINARY),
        "--tycho-url", TYCHO_URL,
        "--auth-key", KEY,
        "--exchange", "uniswap_v2",
        "--exchange", "sushiswap_v2",
        "--exchange", "uniswap_v3",    # Day 5 新增：5/30/100 bps fee tier
        "--chain", "ethereum",
        "--min-tvl", str(MIN_TVL),
    ]

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, bufsize=-1,
    )

    reg         = PoolRegistry()
    graph       = TokenGraph()
    start_time  = time.time()
    block_count = 0
    total_opps  = 0
    all_results = []
    snapshot_done = False

    print(f"{'時間':8}  {'Block':10}  {'池數':5}  {'三角路徑':8}  {'機會':5}  {'最佳 net_usd':>14}")
    print("─" * 70)

    try:
        for raw_line in proc.stdout:
            if time.time() - start_time > scan_seconds:
                break
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                msg = json.loads(raw_line)
            except json.JSONDecodeError:
                continue

            updated = reg.process_message(msg)

            # 快照完成後重建 graph（第一次大量池進來）
            if updated > 10 and not snapshot_done:
                graph.rebuild(reg.pools)
                snapshot_done = True
            elif updated > 0 and snapshot_done:
                # delta 更新後局部重建（只更新有變化的 token）
                graph.rebuild(reg.pools)

            sync  = msg.get("sync_states", {})
            block_num = next(iter(sync.values()), {}).get("number", 0)

            has_deltas = any(
                bool(dm.get("deltas", {}).get("state_updates"))
                for dm in msg.get("state_msgs", {}).values()
            )
            if not has_deltas and block_count > 0:
                continue

            block_count += 1
            n_pools = len(reg.pools)

            # 統計三角路徑數量（取前 5 個已知 token 做樣本）
            known_tokens = [t for t in TOKEN_SHORT if t in graph.edges]
            n_paths = sum(len(graph.find_triangles(t)) for t in known_tokens[:5])

            opps, obs, all_verified = scan_triangles(reg, graph, GAS_COST)
            _db_insert_results(all_verified, block_num)  # Day 13：記錄所有 Quoter 驗證結果
            ts   = datetime.now(timezone.utc).strftime("%H:%M:%S")

            if opps:
                total_opps += len(opps)
                all_results.extend([{**o, "ts": ts, "block": block_num} for o in opps])
                best = opps[0]
                net_disp = best.get("net_real_usd", best["net_star_usd"])
                src      = best.get("source", "model")
                src_tag  = "✅Quoter" if src == "quoter" else "✅v2_model"
                path = f"{short(best['token_a'])}→{short(best['token_b'])}→{short(best['token_c'])}→{short(best['token_a'])}"
                print(f"{ts}  {block_num:10}  {n_pools:5}  {n_paths:8}  {len(opps):5}  ${net_disp:>12.4f}  {src_tag}  {path}")
                if obs:
                    print(f"  └─ OBSERVE(tick_aware=F): {len(obs)} 筆（儲備高估，隔離）")
            else:
                obs_str = f" +{len(obs)} OBSERVE" if obs else ""
                print(f"{ts}  {block_num:10}  {n_pools:5}  {n_paths:8}  {'0':5}  {'—':>14}{obs_str}")

    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()

    elapsed = time.time() - start_time
    print()
    print("=" * 70)
    print(f"  掃描完成  {elapsed:.0f}s / {block_count} blocks")
    print(f"  追蹤池數  : {len(reg.pools)}")
    print(f"  net_star_usd > gas 次數 : {total_opps}")
    if all_results:
        print()
        print("  ✅ 有機會的路徑（Quoter 驗證後）：")
        seen = set()
        for r in sorted(all_results, key=lambda x: -x.get("net_real_usd", x["net_star_usd"]))[:20]:
            path = f"{short(r['token_a'])}→{short(r['token_b'])}→{short(r['token_c'])}"
            if path not in seen:
                seen.add(path)
                net = r.get("net_real_usd", r["net_star_usd"])
                src = r.get("source", "model")
                print(f"    {r['ts']}  block={r['block']}  net=${net:8.4f}  Q*={r['Q_star']:8.0f}  [{src}]  {path}")
    print("=" * 70)
    return all_results


if __name__ == "__main__":
    env_file = Path(__file__).parent.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    scan()
