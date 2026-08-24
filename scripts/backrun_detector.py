"""
scripts/backrun_detector.py — Day 19

策略：監聽 mempool 中的大型 Uniswap swap，
     模擬 swap 執行後的池子狀態，
     計算三角套利的 backrun 機會。

架構：
  1. WSS 訂閱 newPendingTransactions
  2. 拿 full tx → 解析 Uniswap v3 swap calldata
  3. 模擬 swap 後 WETH/USDT pool 的新儲備
  4. 用 Quoter 確認三角套利 net_real（在新池子狀態下）
  5. 如果 net > gas → 印出「BACKRUN 機會！」

最佳路徑（Day 18 確認）：
  DAI → USDT (0.01%) → WETH (0.01%) → DAI (0.05%)
  觸發條件：WETH/USDT 0.01% pool 偏離均衡 ≥ 0.176%

注意：本腳本只「偵測 + 報告」，不實際送交易。
"""

import json
import time
import threading
import urllib.request as _ur
from datetime import datetime

import websocket  # pip install websocket-client

# ── 常數 ─────────────────────────────────────────────
WSS_URL  = "wss://ethereum.publicnode.com"
HTTP_RPC = "https://ethereum.publicnode.com"
RPC_HEADERS = {"User-Agent": "curl/7.88.1", "Content-Type": "application/json"}

# 目標池子地址（Uniswap v3 Ethereum Mainnet）
POOL_WETH_USDT_001 = "0x11b815efb8f581194ae79006d24e0d814b7697f6"  # WETH/USDT 0.01%
POOL_WETH_USDC_001 = "0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640"  # WETH/USDC 0.05% (最深)
POOL_DAI_USDT_001  = "0x48da0965ab2d2cbf1c17c09cfb5cbe67ad5b1406"  # DAI/USDT 0.01%

# Uniswap v3 Router 地址
UNISWAP_V3_ROUTER       = "0xe592427a0aece92de3edee1f18e0157c05861564"
UNISWAP_V3_ROUTER2      = "0x68b3465833fb72a70ecdf485e0e4c7bd8665fc45"
UNISWAP_UNIVERSAL_ROUTER = "0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad"

# Token 地址
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
USDT = "0xdac17f958d2ee523a2206206994597c13d831ec7"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
DAI  = "0x6b175474e89094c44da98b954eedeac495271d0f"

QUOTER_V3 = "0xb27308f9F90D607463bb33eA1BeBb41C27CE5AB6"

# 最小觸發 swap 大小（USD）
MIN_SWAP_USD = 50_000
GAS_COST_USD = 0.09

# ── Uniswap v3 function selectors ────────────────────
# exactInputSingle: 0x414bf389
# exactInput:       0xc04b8d59
# exactOutputSingle:0xdb3e2198
# exactOutput:      0xf28c0498
# multicall:        0xac9650d8 (Universal Router)
SWAP_SELECTORS = {
    "0x414bf389": "exactInputSingle",
    "0xc04b8d59": "exactInput",
    "0xdb3e2198": "exactOutputSingle",
    "0xf28c0498": "exactOutput",
    "0x5ae401dc": "multicall(uint256,bytes[])",   # Router v2 multicall
    "0x1f0464d1": "multicall(bytes32,bytes[])",
    "0x04e45aaf": "exactInputSingle_V2",          # SwapRouter02
    "0x3593564c": "execute",                      # Universal Router
}

TARGET_ROUTERS = {
    UNISWAP_V3_ROUTER.lower(),
    UNISWAP_V3_ROUTER2.lower(),
    UNISWAP_UNIVERSAL_ROUTER.lower(),
}

# ── 工具函數 ──────────────────────────────────────────
def _p32(v: int) -> str:
    return format(int(v), "064x")

def _pa(addr: str) -> str:
    return _p32(int(addr, 16))

def _eth_call(to: str, data: str) -> str:
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "eth_call",
        "params": [{"to": to, "data": data}, "latest"]
    }).encode()
    req  = _ur.Request(HTTP_RPC, data=body, headers=RPC_HEADERS)
    resp = json.loads(_ur.urlopen(req, timeout=8).read())
    if "error" in resp:
        raise RuntimeError(resp["error"])
    return resp["result"]

def _eth_get_tx(txhash: str) -> dict | None:
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": "eth_getTransactionByHash",
        "params": [txhash]
    }).encode()
    req  = _ur.Request(HTTP_RPC, data=body, headers=RPC_HEADERS)
    resp = json.loads(_ur.urlopen(req, timeout=8).read())
    return resp.get("result")

def v3_quote(tin: str, tout: str, fee: int, wei_in: int) -> int:
    cd = ("0xf7729d43"
          + _pa(tin) + _pa(tout)
          + _p32(fee) + _p32(wei_in)
          + _p32(0))
    return int(_eth_call(QUOTER_V3, cd), 16)


# ── Swap 解析 ─────────────────────────────────────────
def parse_swap(tx: dict) -> dict | None:
    """
    從 tx 解析 Uniswap v3 swap 資訊。
    只解析 exactInputSingle（selector 0x414bf389），其他略過。
    回傳 {token_in, token_out, fee, amount_in_wei, router} 或 None。
    """
    to   = (tx.get("to") or "").lower()
    inp  = tx.get("input", "0x")

    if to not in TARGET_ROUTERS:
        return None
    if len(inp) < 10:
        return None

    selector = inp[2:10]

    if selector == "414bf389":
        # exactInputSingle(ExactInputSingleParams params)
        # params: tokenIn(32) tokenOut(32) fee(32) recipient(32)
        #         deadline(32) amountIn(32) amountOutMinimum(32) sqrtPriceLimitX96(32)
        try:
            data = bytes.fromhex(inp[10:])
            if len(data) < 256:
                return None
            token_in  = "0x" + data[12:32].hex()
            token_out = "0x" + data[44:64].hex()
            fee       = int.from_bytes(data[64:96], "big")
            amount_in = int.from_bytes(data[160:192], "big")
            return {
                "token_in":     token_in,
                "token_out":    token_out,
                "fee":          fee,
                "amount_in_wei": amount_in,
                "router":       to,
                "selector":     selector,
            }
        except Exception:
            return None

    elif selector == "04e45aaf":
        # SwapRouter02 exactInputSingle — 同結構
        try:
            data = bytes.fromhex(inp[10:])
            if len(data) < 256:
                return None
            token_in  = "0x" + data[12:32].hex()
            token_out = "0x" + data[44:64].hex()
            fee       = int.from_bytes(data[64:96], "big")
            amount_in = int.from_bytes(data[160:192], "big")
            return {
                "token_in":     token_in,
                "token_out":    token_out,
                "fee":          fee,
                "amount_in_wei": amount_in,
                "router":       to,
                "selector":     selector,
            }
        except Exception:
            return None

    return None


def is_weth_stable_swap(swap: dict) -> bool:
    """判斷是否為 WETH ↔ stable coin 的大額 swap。"""
    tin  = swap["token_in"].lower()
    tout = swap["token_out"].lower()
    stable = {USDT.lower(), USDC.lower(), DAI.lower()}

    return (
        (tin == WETH.lower() and tout in stable) or
        (tout == WETH.lower() and tin in stable)
    )


def estimate_swap_usd(swap: dict) -> float:
    """估算 swap 的 USD 大小（只看 WETH 那側）。"""
    tin  = swap["token_in"].lower()
    tout = swap["token_out"].lower()
    wei  = swap["amount_in_wei"]
    WETH_PRICE = 2400.0  # 近似值

    if tin == WETH.lower():
        return wei / 1e18 * WETH_PRICE
    elif tout == WETH.lower():
        # 輸入是穩定幣
        dec = 6 if tin in {USDT.lower(), USDC.lower()} else 18
        return wei / (10**dec)
    return 0.0


# ── 三角套利驗證 ─────────────────────────────────────
def check_backrun_opportunity(swap: dict, swap_usd: float) -> dict | None:
    """
    在 pending swap 被執行後，驗證最佳三角路徑是否出現機會。

    最佳路徑：DAI→USDT(0.01%)→WETH(0.01%)→DAI(0.05%)
    用 Quoter 在「當前」狀態（pending tx 還沒上鏈）計算，
    如果連「現在」都有機會，backrun 後更有機會。

    注意：真正的 backrun 需要用 eth_call with overrides 模擬 swap 後的狀態，
    但那需要 archive node。這裡用「現在的 Quoter + swap 造成的偏離估算」近似。
    """
    tin  = swap["token_in"].lower()
    tout = swap["token_out"].lower()
    fee  = swap["fee"]

    # 只處理 WETH/USDT 或 WETH/USDC 的 swap
    target_pairs = [
        (WETH.lower(), USDT.lower()),
        (USDT.lower(), WETH.lower()),
        (WETH.lower(), USDC.lower()),
        (USDC.lower(), WETH.lower()),
    ]
    if (tin, tout) not in target_pairs:
        return None

    try:
        # 最佳路徑：DAI→USDT(0.01%)→WETH(0.01%)→DAI(0.05%)
        # 嘗試 Q=$100 和 Q=$500
        results = []
        for q_usd in [50, 100, 200, 500]:
            w = int(q_usd * 1e18)
            b = v3_quote(DAI, USDT, 100, w)
            c = v3_quote(USDT, WETH, 100, b)
            a = v3_quote(WETH, DAI, 500, c)
            net = (a - w) / 1e18
            results.append((q_usd, net))

        best_q, best_net = max(results, key=lambda x: x[1])

        # 反向路徑也試試
        results_rev = []
        for q_usd in [50, 100, 200, 500]:
            w = int(q_usd * 1e18)
            b = v3_quote(DAI, WETH, 500, w)
            c = v3_quote(WETH, USDT, 100, b)
            a = v3_quote(USDT, DAI, 100, c)
            net = (a - w) / 1e18
            results_rev.append((q_usd, net))

        best_q_rev, best_net_rev = max(results_rev, key=lambda x: x[1])

        return {
            "swap_usd":     swap_usd,
            "swap_pair":    f"{tin[:8]}→{tout[:8]}",
            "swap_fee":     fee,
            "fwd_best_q":   best_q,
            "fwd_net":      best_net,
            "fwd_go":       best_net > GAS_COST_USD,
            "rev_best_q":   best_q_rev,
            "rev_net":      best_net_rev,
            "rev_go":       best_net_rev > GAS_COST_USD,
            "all_results":  results,
            "all_results_rev": results_rev,
        }
    except Exception as e:
        return {"error": str(e)}


# ── 主迴圈 ────────────────────────────────────────────
processed = 0
hits = 0
opportunities = 0
lock = threading.Lock()


def process_tx(txhash: str):
    global processed, hits, opportunities

    try:
        tx = _eth_get_tx(txhash)
        if not tx:
            return

        swap = parse_swap(tx)
        if not swap:
            return

        if not is_weth_stable_swap(swap):
            return

        swap_usd = estimate_swap_usd(swap)
        if swap_usd < MIN_SWAP_USD:
            return

        with lock:
            hits += 1

        ts = datetime.now().strftime("%H:%M:%S")
        tin_name  = {WETH.lower():"WETH", USDT.lower():"USDT",
                     USDC.lower():"USDC", DAI.lower():"DAI"}.get(swap["token_in"].lower(),  swap["token_in"][:8])
        tout_name = {WETH.lower():"WETH", USDT.lower():"USDT",
                     USDC.lower():"USDC", DAI.lower():"DAI"}.get(swap["token_out"].lower(), swap["token_out"][:8])

        print(f"\n{'─'*65}")
        print(f"[{ts}] 🎯 大 swap 偵測！")
        print(f"  tx:     {txhash}")
        print(f"  swap:   {tin_name} → {tout_name}  fee={swap['fee']/1e4:.2f}%  ~${swap_usd:,.0f}")

        result = check_backrun_opportunity(swap, swap_usd)
        if not result:
            print(f"  ⚠️  非目標池子，跳過")
            return
        if "error" in result:
            print(f"  ❌ Quoter 錯誤：{result['error']}")
            return

        print(f"  正向套利  Q=${result['fwd_best_q']}: net={result['fwd_net']:>+.4f} USD  {'✅ GO!' if result['fwd_go'] else '❌'}")
        print(f"  反向套利  Q=${result['rev_best_q']}: net={result['rev_net']:>+.4f} USD  {'✅ GO!' if result['rev_go'] else '❌'}")
        print(f"  gas cost: ${GAS_COST_USD:.2f}")

        if result["fwd_go"] or result["rev_go"]:
            with lock:
                opportunities += 1
            direction = "正向" if result["fwd_go"] else "反向"
            best_net  = result["fwd_net"] if result["fwd_go"] else result["rev_net"]
            best_q    = result["fwd_best_q"] if result["fwd_go"] else result["rev_best_q"]
            print(f"\n  🚀🚀🚀 BACKRUN 機會！{direction} Q=${best_q}  net-gas=${best_net - GAS_COST_USD:>+.4f}")
            print(f"  路徑：DAI→USDT(0.01%)→WETH(0.01%)→DAI(0.05%)")

    except Exception as e:
        pass  # 靜默跳過（大量 tx，錯誤是正常的）
    finally:
        with lock:
            processed += 1


def run(duration_sec: int = 300):
    """執行 backrun 偵測，持續 duration_sec 秒。"""
    print("=" * 65)
    print("  Day 19 — Backrun Detector")
    print(f"  目標：WETH/USDT 或 WETH/USDC 大 swap（>${MIN_SWAP_USD:,}）")
    print(f"  最佳路徑：DAI→USDT(0.01%)→WETH(0.01%)→DAI(0.05%)")
    print(f"  執行時長：{duration_sec}s")
    print("=" * 65)

    sub_id = None
    tx_queue = []
    queue_lock = threading.Lock()

    def on_message(ws, msg):
        data = json.loads(msg)
        if "params" in data:
            txhash = data["params"]["result"]
            with queue_lock:
                tx_queue.append(txhash)

    def on_open(ws):
        nonlocal sub_id
        ws.send(json.dumps({
            "jsonrpc": "2.0", "id": 1,
            "method": "eth_subscribe",
            "params": ["newPendingTransactions"]
        }))
        print("✅ 已訂閱 newPendingTransactions")

    def on_error(ws, err):
        print(f"WSS 錯誤：{err}")

    ws = websocket.WebSocketApp(
        WSS_URL,
        on_message=on_message,
        on_open=on_open,
        on_error=on_error,
    )
    ws_thread = threading.Thread(target=ws.run_forever, kwargs={"ping_interval": 20})
    ws_thread.daemon = True
    ws_thread.start()

    start = time.time()
    workers = []

    print(f"\n{'時間':<10}  {'已掃 tx':>8}  {'大 swap':>8}  {'機會':>6}")
    print("─" * 40)

    try:
        while time.time() - start < duration_sec:
            # 批次處理 queue
            batch = []
            with queue_lock:
                batch = tx_queue[:20]
                tx_queue[:20] = []

            for txhash in batch:
                t = threading.Thread(target=process_tx, args=(txhash,))
                t.daemon = True
                t.start()
                workers.append(t)

            # 每 15 秒印一次進度
            elapsed = int(time.time() - start)
            if elapsed % 15 == 0 and elapsed > 0:
                ts = datetime.now().strftime("%H:%M:%S")
                with lock:
                    print(f"{ts}  {processed:>8}  {hits:>8}  {opportunities:>6}")

            time.sleep(0.5)

    except KeyboardInterrupt:
        pass
    finally:
        ws.close()

    # 等待 worker 完成
    for t in workers[-50:]:
        t.join(timeout=2)

    elapsed = time.time() - start
    print(f"\n{'='*65}")
    print(f"  完成  {elapsed:.0f}s")
    print(f"  掃描 tx 數：{processed}")
    print(f"  大 swap 數：{hits}（>${MIN_SWAP_USD:,}）")
    print(f"  backrun 機會：{opportunities}")
    print(f"{'='*65}")


if __name__ == "__main__":
    import sys
    duration = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    run(duration)
