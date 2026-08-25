# Day 20 — MEV-Share 接通 + State Override 模擬 + AMM 數學確認機會存在

日期：2026-08-25

---

## 1. P0：Flashbots MEV-Share SSE Stream

**狀態：✅ 完全可用（免費，無需 API key）**

```bash
curl -s -H "Accept: text/event-stream" \
  "https://mev-share.flashbots.net/api/v1/events"
```

### MEV-Share Event 格式

每個 event 包含：
- `hash`：tx hash（private，打亂過）
- `logs`：部分 log（帶 address + topics，可識別 pool）
- `txs`：（有時）部分 calldata（`to` + `functionSelector`）

**重要**：MEV-Share 的 tx 不是 private mempool 的全部——
Flashbots 會 hash 某些欄位以保護用戶隱私。
我們能從 logs 裡看到 Uniswap Swap event（含 pool address + amount0/1），
**但不一定能算出精確的 amount（某些被打亂）**。

### 觀察到的 tx 類型（5 秒 stream 樣本）

```
0x3da8cd... USDT→某token swap (pool 0x3902..., Swap event 有 amount)
0x93f92a... PENDLE→WETH swap (pool 0x57af...)
0x9cd3e1... USDT→某token swap (4d68b5 pool)
```

→ **MEV-Share 確實有 Uniswap Swap 的 pending log，包含 pool address 和 amount**

---

## 2. P1：State Override 模擬

### QuoterV2（0x61fFE014...）成功取得 sqrtPriceX96After

```python
# $100,000 USDT→WETH（0.01% pool）的 price impact
whale = 100_000
r = quoter_v2(USDT, WETH, 100, int(whale*1e6))
# amountOut: 39.04 WETH
# sqrtPriceX96After: 對應 $2,613.54 USDT/WETH
# price impact: +4.125%
```

### State Override 的限制

**publicnode.com 免費 RPC 不完整支援 stateDiff override**：
- 送出 eth_call with `{stateDiff: {...}}` — 伺服器接受
- 但 Quoter 的輸出和沒有 override 完全相同
- 推斷：節點不支援 stateDiff（可能只支援 state 而非 stateDiff）

要真正模擬 swap 後狀態，需要：
- Alchemy / Infura（付費）
- 或本地節點（geth / reth）

### AMM 數學估算（替代方案）

不依賴 Quoter，直接用公式計算：

```python
# Whale $100k USDT→WETH (+4.1% price impact)
P_before = 2510  # WETH/USDT
P_after  = 2613  # whale 後

# 反向路徑：DAI→WETH(0.05%)→USDT(0.01%)→DAI(0.01%) Q=$100
weth = Q / P_before * (1 - 0.05%)   # Leg1（DAI/WETH pool，不受 whale 影響）
usdt = weth * P_after * (1 - 0.01%) # Leg2（高 price 賣 WETH）
dai_out = usdt * (1 - 0.01%)        # Leg3

net = dai_out - Q = +$4.03  ← 正 EV！
```

---

## 3. 核心量化結論

**AMM 數學確認：backrun 機會存在！**

| Whale 規模 | Price Impact | Q=$100 net | 扣 gas 後 |
|-----------|-------------|-----------|---------|
| $50,000  | +2.0% | **+$1.98** | **+$1.89** 🚀 |
| $100,000 | +4.1% | **+$4.03** | **+$3.94** 🚀 |
| $200,000 | +8.2% | **+$8.12** | **+$8.03** 🚀 |

**每 10 分鐘約有 5 筆 $50k+ swap** → 每 10 分鐘約 5 次 backrun 機會

### 為什麼 Quoter 算不出來？

| 層次 | 說明 |
|------|------|
| Quoter 查 latest block | 已是 MEV 清場後 |
| State override | publicnode 不支援 |
| AMM 數學 | **✅ 正確！但不考慮 tick 邊界** |

---

## 4. 21 天學習成果量化

### 建立的系統

| 模組 | 功能 | 狀態 |
|------|------|------|
| `models/ev_model.py` | AMM EV 模型（8/8 verify） | ✅ 完成 |
| `scripts/tri_scanner.py` | Tycho WebSocket 三角掃描 | ✅ 完成 |
| `scripts/quoter_q_scan.py` | Quoter Q 範圍掃描 | ✅ 完成 |
| `scripts/backrun_detector.py` | MEV-Share + Swap 偵測 | ✅ 完成 |
| `data/arb.duckdb` | 2,688 筆 Quoter 記錄 | ✅ 完成 |

### 關鍵發現（按時序）

1. **Day 1-9**：EV 模型 ✅，r* 和 bribe 策略
2. **Day 10-14**：Tycho 掃描器，發現 Quoter 和 AMM 模型的 gap（$3,938 vs -$1）
3. **Day 15**：Dencun 後 gas = $0.09（不是 $5.50）
4. **Day 16-17**：v3 tick 幻覺根因確認，Quoter Q 掃描
5. **Day 18**：最佳路徑定位（DAI→USDT→WETH→DAI, 0.01%+0.01%+0.05%），break-even 差 0.176%
6. **Day 19**：mempool 接通，大 swap 頻率（每 block ~1 筆 $10k+）
7. **Day 20**：MEV-Share 接通，AMM 數學確認 backrun 機會（+$4 at whale $100k）

### 剩餘差距

| 差距 | 說明 | 解法 |
|------|------|------|
| State override 限制 | 需要 Alchemy/Infura | 付費 RPC 或本地節點 |
| Tick 邊界 | AMM 數學忽略 tick 邊界，實際可能少 30% | 用 QuoterV2 sqrtPriceAfter |
| 執行速度 | Python 計算 + 公共 RPC 延遲 | 需要 Rust + private RPC |
| Bundle 送出 | 未實作 Flashbots bundle 送出 | Flashbots bundle API |
| 智能合約 | 無原子 backrun 合約 | Solidity 合約 |

---

## 5. Day 21 計畫（最後一天）

- [ ] **P0**：整合 MEV-Share + AMM 數學 → 完整 backrun pipeline（只偵測，不執行）
- [ ] **P1**：量測延遲：從 MEV-Share event 收到到計算完成需要多少毫秒？
- [ ] **P2**：21 天完整總結文章
