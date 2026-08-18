"""
tycho_scanner.py — 全池掃描版（Day 4 選項 A）

相對於 Day 3 版本的改動：
  - 移除 USDC/WETH 限制，接受所有幣對
  - PoolRegistry 改用 (token_a, token_b) 分組，同幣對跨 DEX 才互比
  - 擴大 exchange 清單：uniswap_v2 + sushiswap_v2 + uniswap_v3
  - 新增常見 token decimals 表（預填 ~30 個主流 token）
  - scan_opportunities 改為「按幣對分組，跨 DEX pairwise」

# ⚠️ PoolRegistry / hex_to_int / decimals 表與 tri_scanner.py 重複
#    見 notes/day07_review.md — 第三次踩到同一個坑時再抽共用模組

Schema（實測 2026-08-07）：
  stdout JSON per block：
    state_msgs[dex] → {
      snapshots: {
        "states": { pool_addr: { state: {attributes: {reserve0, reserve1}}, component: {...} } }
        "components": { pool_addr: component_dict }
      },
      deltas: { pool_addr: { state: {updated_attributes: {reserve0, reserve1}} } }
    }
  sync_states[dex] → {status, number, ...}
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from models.ev_model import PoolState, ChainParams, optimal_size

# ── 設定 ──────────────────────────────────────
KEY       = os.environ.get("TYCHO_API_KEY", "")
TYCHO_URL = "tycho-fynd-ethereum.propellerheads.xyz:443"
BINARY    = Path(__file__).parent / "tycho-client"

# Day 9：切換到 bundle venue（f_cost=0，失敗不上鏈）
# 判斷邏輯簡化：net_raw > gas_cost → go（不再依賴 sigmoid）
# bundle 下 revert_gas 仍保留（雖然 f_cost=0，gas 本身仍要付）
CHAIN    = ChainParams(venue="bundle", base_gas_usd=4.0, priority_fee_usd=1.5)
GAS_COST = CHAIN.base_gas_usd + CHAIN.priority_fee_usd  # $5.5

SCAN_SECONDS = int(os.environ.get("SCAN_SECONDS", "3600"))

# 只輸出 net_star 超過這個門檻的機會（過濾雜訊）
MIN_NET_STAR = GAS_COST


# ── 常見 token decimals（預填，避免 RPC 查詢）──
# 沒在這裡的 token 預設 18
TOKEN_DECIMALS: dict[str, int] = {
    # Stablecoins
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": 6,   # USDC
    "0xdac17f958d2ee523a2206206994597c13d831ec7": 6,   # USDT
    "0x4fabb145d64652a948d72533023f6e7a623c7c53": 18,  # BUSD
    "0x6b175474e89094c44da98b954eedeac495271d0f": 18,  # DAI
    "0x0000000000085d4780b73119b644ae5ecd22b376": 18,  # TUSD
    "0x956f47f50a910163d8bf957cf5846d573e7f87ca": 18,  # FEI
    "0x853d955acef822db058eb8505911ed77f175b99e": 18,  # FRAX

    # ETH variants
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": 18,  # WETH
    "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599": 8,   # WBTC
    "0x7d1afa7b718fb893db30a3abc0cfc608aacfebb0": 18,  # MATIC
    "0x514910771af9ca656af840dff83e8264ecf986ca": 18,  # LINK
    "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984": 18,  # UNI
    "0xba100000625a3754423978a60c9317c58a424e3d": 18,  # BAL
    "0xc00e94cb662c3520282e6f5717214004a7f26888": 18,  # COMP
    "0x9f8f72aa9304c8b593d555f12ef6589cc3a579a2": 18,  # MKR
    "0x6810e776880c02933d47db1b9fc05908e5386b96": 18,  # GNO
    "0x111111111117dc0aa78b770fa6a738034120c302": 18,  # 1INCH
    "0xd533a949740bb3306d119cc777fa900ba034cd52": 18,  # CRV
    "0xde30da39c46104798bb5aa3fe8b9e0e1f348163f": 18,  # GTC
    "0x4e3fbd56cd56c3e72c1403e103b45db9da5b9d2b": 18,  # CVX
    "0xae7ab96520de3a18e5e111b5eaab095312d7fe84": 18,  # stETH
    "0x5a98fcbea516cf06857215779fd812ca3bef1b32": 18,  # LDO
    "0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9": 18,  # AAVE
    "0xd31a59c85ae9d8edefec411d448f90841571b89c": 9,   # SOL (wrapped)
    "0x0bc529c00c6401aef6d220be8c6ea1667f6ad93e": 18,  # YFI
    "0xe41d2489571d322189246dafa5ebde1f4699f498": 18,  # ZRX
    "0x6f259637dcd74c767781e37bc6133cd6a68aa161": 18,  # HT
    "0x408e41876cccdc0f92210600ef50372656052a38": 18,  # REN
}

# token 的 USD 價格（用於 optimal_size 的 price_x 參數，確保方向比較用 USD）
# ⚠️ 固定值，僅用於方向選擇，不影響套利金額計算
TOKEN_USD_PRICE: dict[str, float] = {
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": 1.0,      # USDC
    "0xdac17f958d2ee523a2206206994597c13d831ec7": 1.0,      # USDT
    "0x6b175474e89094c44da98b954eedeac495271d0f": 1.0,      # DAI
    "0x4fabb145d64652a948d72533023f6e7a623c7c53": 1.0,      # BUSD
    "0x853d955acef822db058eb8505911ed77f175b99e": 1.0,      # FRAX
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": 3500.0,   # WETH
    "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599": 95000.0,  # WBTC
    "0x7d1afa7b718fb893db30a3abc0cfc608aacfebb0": 0.5,      # MATIC
    "0x514910771af9ca656af840dff83e8264ecf986ca": 15.0,     # LINK
    "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984": 10.0,     # UNI
    "0x5a98fcbea516cf06857215779fd812ca3bef1b32": 1.5,      # LDO
    "0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9": 100.0,    # AAVE
}

KNOWN_PAIRS_LABEL: dict[frozenset, str] = {
    frozenset(["0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
               "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"]): "USDC/WETH",
    frozenset(["0xdac17f958d2ee523a2206206994597c13d831ec7",
               "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"]): "USDT/WETH",
    frozenset(["0x6b175474e89094c44da98b954eedeac495271d0f",
               "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"]): "DAI/WETH",
    frozenset(["0x2260fac5e5542a773aa44fbcfedf7c193bc2c599",
               "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"]): "WBTC/WETH",
    frozenset(["0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
               "0xdac17f958d2ee523a2206206994597c13d831ec7"]): "USDC/USDT",
    frozenset(["0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
               "0x6b175474e89094c44da98b954eedeac495271d0f"]): "USDC/DAI",
}


def get_decimals(token_addr: str) -> int:
    return TOKEN_DECIMALS.get(token_addr.lower(), 18)


def hex_to_int(val) -> int | None:
    if val is None:
        return None
    if isinstance(val, int):
        return val
    s = str(val)
    if s.startswith("0x") or s.startswith("0X"):
        return int(s, 16)
    try:
        return int(s)
    except ValueError:
        return None


# ── PoolRegistry（全幣對版）────────────────────

class PoolRegistry:
    """
    維護所有池的狀態，按 (token_a, token_b) 分組。

    self.meta[pool_addr] = {t0, t1, d0, d1, fee, dex, pair_key}
    self.pools[pool_addr] = {r0, r1, fee, dex, t0, t1}
    self.pair_to_pools[(ta, tb)] = [pool_addr, ...]  # 同幣對的池清單
    """

    def __init__(self):
        self.meta:  dict[str, dict] = {}
        self.pools: dict[str, dict] = {}
        self.pair_to_pools: dict[tuple, list[str]] = defaultdict(list)

    def register_component(self, addr: str, comp: dict) -> bool:
        """
        從 component dict 提取 token 順序、fee、dex。
        接受所有幣對（移除 USDC/WETH 限制）。
        """
        tokens = comp.get("tokens", [])
        if len(tokens) < 2:
            return False

        t0 = tokens[0].lower()
        t1 = tokens[1].lower()
        if not t0 or not t1 or t0 == t1:
            return False

        sa = comp.get("static_attributes", {})
        fee_hex = sa.get("fee", "0x1e")  # 0x1e = 30 bps
        fee_int = hex_to_int(fee_hex) or 30
        dex = comp.get("protocol_system", "?")

        # v2/sushiswap: fee 單位是 bps（/10_000），例如 30 → 0.003
        # v3: fee 單位是 pips（/1_000_000），例如 500 → 0.0005
        # ⚠️ Day 5 踩坑：v3 的 500 被誤算成 fee=5%（/10_000），正確應是 0.05%（/1_000_000）
        if "uniswap_v3" in dex.lower() or "v3" in dex.lower():
            fee = fee_int / 1_000_000
        else:
            fee = fee_int / 10_000

        # 正規化 pair_key：addresses 排序，確保同幣對不同順序的池能合在一起
        pair_key = tuple(sorted([t0, t1]))

        self.meta[addr] = {
            "t0": t0, "t1": t1,
            "d0": get_decimals(t0),
            "d1": get_decimals(t1),
            "fee": fee,
            "dex": dex,
            "pair_key": pair_key,
        }

        if addr not in self.pair_to_pools[pair_key]:
            self.pair_to_pools[pair_key].append(addr)

        return True

    def update_state(self, addr: str, attrs: dict) -> bool:
        """
        更新池狀態：v2 讀 reserve0/reserve1；v3 從 sqrt_price/liquidity 推虛擬儲備。

        ⚠️ v3 虛擬儲備警告：r0=L/sqrtP, r1=L*sqrtP 忽略 tick range，
           在 price 接近 tick 邊界時可能高估數億倍（RAIL/USDT 坑，Day 6）。
           此處儲存的 r0/r1 僅用於快篩排序，不作為精確模擬輸入。
           Day 8+ 改用 Tycho Simulation 做精確算法。
        """
        meta = self.meta.get(addr)
        if meta is None:
            return False

        dex = meta.get("dex", "")
        is_v3 = "v3" in dex.lower()

        if is_v3:
            # v3：從 sqrt_price_x96 + liquidity + tick range 算 tick-aware 虛擬儲備
            sqrtp_raw = (attrs.get("sqrt_price_x96") or attrs.get("sqrtPriceX96")
                         or attrs.get("sqrt_price"))
            liq_raw   = attrs.get("liquidity")
            tick_raw  = attrs.get("tick")
            if sqrtp_raw is None or liq_raw is None:
                return False

            sqrtp_int = hex_to_int(sqrtp_raw)
            liq_int   = hex_to_int(liq_raw)
            if sqrtp_int is None or liq_int is None or sqrtp_int == 0 or liq_int == 0:
                return False

            sqrtP = sqrtp_int / (2 ** 96)
            L = liq_int

            # 解析 current tick（Tycho 以 hex 24-bit 有號整數傳送）
            current_tick = None
            if tick_raw is not None:
                t_int = hex_to_int(tick_raw)
                if t_int is not None:
                    # 24-bit 有號：≥ 2^23 代表負數
                    current_tick = t_int - (2 ** 24) if t_int >= 2 ** 23 else t_int

            # 收集所有初始化 tick（key 格式："ticks/{N}/net-liquidity"）
            init_ticks = []
            for k in attrs:
                if k.startswith("ticks/") and k.endswith("/net-liquidity"):
                    try:
                        init_ticks.append(int(k.split("/")[1]))
                    except ValueError:
                        pass

            # 找 tick_lower / tick_upper（current tick 左右各最近的已初始化 tick）
            tick_lower = tick_upper = None
            if current_tick is not None and init_ticks:
                below = [t for t in init_ticks if t <= current_tick]
                above = [t for t in init_ticks if t > current_tick]
                if below:
                    tick_lower = max(below)
                if above:
                    tick_upper = min(above)

            # tick-aware 公式（正確）
            # r0 = L * (sqrtP_upper - sqrtP) / (sqrtP * sqrtP_upper)
            # r1 = L * (sqrtP - sqrtP_lower)
            # 退化到舊公式（僅快篩用，精度差）：r0=L/sqrtP, r1=L*sqrtP
            tick_aware = False
            if tick_lower is not None and tick_upper is not None:
                try:
                    sqrtP_lower = math.sqrt(1.0001 ** tick_lower)
                    sqrtP_upper = math.sqrt(1.0001 ** tick_upper)
                    if sqrtP_lower < sqrtP < sqrtP_upper and sqrtP * sqrtP_upper > 0:
                        r0_raw_val = L * (sqrtP_upper - sqrtP) / (sqrtP * sqrtP_upper)
                        r1_raw_val = L * (sqrtP - sqrtP_lower)
                        tick_aware = True
                except (OverflowError, ZeroDivisionError):
                    pass

            if not tick_aware:
                # 退化：舊公式（忽略 tick range，快篩用）
                # ⚠️ 接近 tick 邊界時可能高估數億倍（RAIL/USDT 坑，Day 6）
                r0_raw_val = L / sqrtP
                r1_raw_val = L * sqrtP

            r0_raw_val /= 10 ** meta["d0"]
            r1_raw_val /= 10 ** meta["d1"]

            if r0_raw_val <= 0 or r1_raw_val <= 0:
                return False

            self.pools[addr] = {
                "r0": r0_raw_val, "r1": r1_raw_val,
                "fee": meta["fee"],
                "dex": dex,
                "t0": meta["t0"], "t1": meta["t1"],
                "pair_key": meta["pair_key"],
                "is_v3": True,
                "tick_aware": tick_aware,   # False = 退化到舊公式，掃描時可跳過精度敏感邏輯
            }
            return True

        else:
            # v2 / sushiswap：直接讀 reserve0/reserve1
            r0_raw = attrs.get("reserve0") or attrs.get("Reserve0")
            r1_raw = attrs.get("reserve1") or attrs.get("Reserve1")
            if r0_raw is None or r1_raw is None:
                return False

            r0_int = hex_to_int(r0_raw)
            r1_int = hex_to_int(r1_raw)
            if r0_int is None or r1_int is None:
                return False

            r0 = r0_int / 10 ** meta["d0"]
            r1 = r1_int / 10 ** meta["d1"]

            if r0 <= 0 or r1 <= 0:
                return False

            self.pools[addr] = {
                "r0": r0, "r1": r1,
                "fee": meta["fee"],
                "dex": dex,
                "t0": meta["t0"], "t1": meta["t1"],
                "pair_key": meta["pair_key"],
                "is_v3": False,
            }
            return True

    def n_pools(self) -> int:
        return len(self.pools)

    def n_pairs(self) -> int:
        return sum(1 for addrs in self.pair_to_pools.values()
                   if any(a in self.pools for a in addrs) and len([a for a in addrs if a in self.pools]) >= 2)


def get_pool_states_for_pair(
    reg: PoolRegistry, addr_a: str, addr_b: str
) -> tuple[PoolState, PoolState, PoolState, PoolState] | None:
    """
    回傳 (buy_a, sell_a, buy_b, sell_b)。
    pair_key 決定 r0/r1 對應哪個 token，pair_key[0] 是「token_x」（排序後的低地址）。
    buy_x = PoolState(x=r_tokenX, y=r_tokenY)  → 投入 tokenX，買出 tokenY
    sell_x = PoolState(x=r_tokenY, y=r_tokenX) → 投入 tokenY，買出 tokenX
    """
    pa = reg.pools.get(addr_a)
    pb = reg.pools.get(addr_b)
    if pa is None or pb is None:
        return None

    # pair_key[0] = 較低地址的 token（即 t0 若 t0 < t1，否則 t1）
    # pa["t0"] 是 Tycho 給的 token0，不一定等於 pair_key[0]
    pk = pa["pair_key"]
    if pa["t0"] == pk[0]:
        ra_x, ra_y = pa["r0"], pa["r1"]   # r_tokenX, r_tokenY
    else:
        ra_x, ra_y = pa["r1"], pa["r0"]

    if pb["t0"] == pk[0]:
        rb_x, rb_y = pb["r0"], pb["r1"]
    else:
        rb_x, rb_y = pb["r1"], pb["r0"]

    buy_a  = PoolState(x=ra_x, y=ra_y, fee=pa["fee"])  # tokenX → tokenY
    sell_a = PoolState(x=ra_y, y=ra_x, fee=pa["fee"])  # tokenY → tokenX
    buy_b  = PoolState(x=rb_x, y=rb_y, fee=pb["fee"])
    sell_b = PoolState(x=rb_y, y=rb_x, fee=pb["fee"])

    return buy_a, sell_a, buy_b, sell_b


def process_message(msg: dict, reg: PoolRegistry) -> int:
    updated = 0
    state_msgs = msg.get("state_msgs", {})

    for dex, dex_msg in state_msgs.items():
        snaps = dex_msg.get("snapshots", {})
        states_dict = snaps.get("states", {})
        comps_dict  = snaps.get("components", {})

        for addr, entry in states_dict.items():
            addr = addr.lower()
            comp = entry.get("component", {})
            if comp:
                reg.register_component(addr, comp)
            attrs = entry.get("state", {}).get("attributes", {})
            if reg.update_state(addr, attrs):
                updated += 1

        for addr, comp in comps_dict.items():
            addr = addr.lower()
            reg.register_component(addr, comp)

        deltas = dex_msg.get("deltas", {})
        state_updates = deltas.get("state_updates", {})
        for addr, upd in state_updates.items():
            if not isinstance(upd, dict):
                continue
            addr = addr.lower()
            attrs = upd.get("updated_attributes", {})
            if reg.update_state(addr, attrs):
                updated += 1

    return updated


def scan_opportunities(reg: PoolRegistry) -> list[dict]:
    """
    全池掃描：
    - 按 pair_key 分組，同幣對才互比
    - 每組內做 pairwise：A→B 和 B→A 兩個方向
    - 只保留 net_star > MIN_NET_STAR 的結果
    - tick_aware=False 的 v3 池進 OBSERVE（儲備高估，不進主排序）
      ⚠️ 875 個 tick_aware + 72 個 !tick_aware 混排，那 72 個儲備偏大會系統性排前面
    """
    results   = []   # 正常結果（進排序）
    observe   = []   # tick_aware=False 的池（隔離，僅供觀察）

    for pair_key, addrs in reg.pair_to_pools.items():
        # 只取有 state 的池
        live = [a for a in addrs if a in reg.pools]
        if len(live) < 2:
            continue

        # 幣對標籤
        pair_label = KNOWN_PAIRS_LABEL.get(frozenset(pair_key), f"{pair_key[0][:6]}…/{pair_key[1][:6]}…")

        for i, addr_a in enumerate(live):
            for addr_b in live[i+1:]:
                states = get_pool_states_for_pair(reg, addr_a, addr_b)
                if states is None:
                    continue
                buy_a, sell_a, buy_b, sell_b = states

                dex_a = reg.pools[addr_a]["dex"]
                dex_b = reg.pools[addr_b]["dex"]

                # tick_aware 標記：任一池 False 就進 OBSERVE
                ta_a = reg.pools[addr_a].get("tick_aware", True)
                ta_b = reg.pools[addr_b].get("tick_aware", True)
                to_observe = not (ta_a and ta_b)

                # pool_a.x token 的 USD 價格，傳給 optimal_size 做方向比較
                t0_a = reg.pools[addr_a]["t0"]
                price_x = TOKEN_USD_PRICE.get(t0_a, 1.0)

                def _try_dir(pa, pb, dir_label, addr_x, addr_y):
                    try:
                        res = optimal_size(pa, pb, price_x=price_x)
                        if res["direction"] == "no_opportunity" or res["net_star"] <= MIN_NET_STAR:
                            return
                        entry = {
                            "pair":       pair_label,
                            "dir":        dir_label,
                            "addr_a":     addr_x,
                            "addr_b":     addr_y,
                            "net_star":   res["net_star"],
                            "net_star_usd": round(res["net_star_usd"], 4),
                            "Q_star":     res["Q_star"],
                            "tick_aware": not to_observe,
                        }
                        if to_observe:
                            observe.append(entry)
                        else:
                            results.append(entry)
                    except Exception:
                        pass

                _try_dir(buy_a, sell_b, f"{dex_a}→{dex_b}", addr_a, addr_b)
                _try_dir(buy_b, sell_a, f"{dex_b}→{dex_a}", addr_b, addr_a)

    # 正常結果按 net_star_usd 降序；OBSERVE 另排
    results.sort(key=lambda x: x["net_star_usd"], reverse=True)
    observe.sort(key=lambda x: x["net_star_usd"], reverse=True)
    return results, observe


def scan(scan_seconds: int = SCAN_SECONDS):
    if not KEY:
        print("❌ TYCHO_API_KEY 未設定", file=sys.stderr)
        sys.exit(1)

    print(f"🔍 Tycho 全池掃描器（Day 4 版）")
    print(f"   掃描時長 : {scan_seconds}s")
    print(f"   Gas 門檻 : net_star > ${MIN_NET_STAR:.2f}")
    print()

    cmd = [
        str(BINARY),
        "--tycho-url",  TYCHO_URL,
        "--auth-key",   KEY,
        "--exchange",   "uniswap_v2",
        "--exchange",   "sushiswap_v2",
        "--exchange",   "uniswap_v3",    # Day 4 新增
        "--chain",      "ethereum",
        "--min-tvl",    "10",
    ]

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, bufsize=1,
    )

    reg         = PoolRegistry()
    start_time  = time.time()
    block_count = 0
    total_opps  = 0
    all_results = []

    print(f"{'時間':8}  {'Block':10}  {'總池數':6}  {'有機會幣對':8}  {'機會':5}  {'最佳 net_star':>14}  最佳幣對")
    print("─" * 90)

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

            process_message(msg, reg)

            sync = msg.get("sync_states", {})
            block_num = 0
            for sv in sync.values():
                block_num = sv.get("number", 0)
                break

            has_deltas = any(
                bool(dm.get("deltas", {}).get("state_updates"))
                for dm in msg.get("state_msgs", {}).values()
            )
            if not has_deltas and block_count > 0:
                continue

            block_count += 1
            n_pools = reg.n_pools()
            n_pairs = reg.n_pairs()
            opps, obs = scan_opportunities(reg)
            ts      = datetime.now(timezone.utc).strftime("%H:%M:%S")

            if opps:
                total_opps += len(opps)
                all_results.extend([{**o, "ts": ts, "block": block_num} for o in opps])
                best = opps[0]
                print(f"{ts}  {block_num:10}  {n_pools:6}  {n_pairs:8}  {len(opps):5}  ${best['net_star_usd']:>10.4f}  {best['pair']} ({best['dir']})")
                if obs:
                    print(f"  └─ OBSERVE(tick_aware=F): {len(obs)} 筆（儲備高估，隔離）")
            else:
                obs_str = f" +{len(obs)} OBSERVE" if obs else ""
                print(f"{ts}  {block_num:10}  {n_pools:6}  {n_pairs:8}  {'0':5}  {'—':>12}{obs_str}")

    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()

    elapsed = time.time() - start_time
    print()
    print("=" * 90)
    print(f"  掃描完成  {elapsed:.0f}s / {block_count} blocks")
    print(f"  追蹤池總數        : {reg.n_pools()}")
    print(f"  有 ≥2 池的幣對數  : {reg.n_pairs()}")
    print(f"  net_star > gas 次數: {total_opps}")

    if all_results:
        # 統計出現最多的幣對
        from collections import Counter
        pair_counts = Counter(r["pair"] for r in all_results)
        print()
        print("  出現頻率最高的幣對（機會數）：")
        for pair, cnt in pair_counts.most_common(10):
            best_net = max(r["net_star"] for r in all_results if r["pair"] == pair)
            print(f"    {pair:20s}  {cnt:4d} 次  最佳 net=${best_net:.4f}")

        print()
        print("  最大 net_star 前 10 筆：")
        top10 = sorted(all_results, key=lambda x: x["net_star"], reverse=True)[:10]
        for r in top10:
            print(f"    {r['ts']}  block={r['block']:10}  net=${r['net_star']:8.4f}  Q*={r['Q_star']:8.0f}  {r['pair']} ({r['dir']})")
    print("=" * 90)
    return all_results


if __name__ == "__main__":
    env_file = Path(__file__).parent.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

    scan()
