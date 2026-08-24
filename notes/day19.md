# Day 19 — Backrun Detector：接通 mempool，確認機會頻率

日期：2026-08-24

---

## 1. 今日工作

**P0**：確認 mempool WSS 可用 → ✅
**P1**：建 `scripts/backrun_detector.py` → ✅
**P2**：執行 real-time monitor，量化大 swap 頻率 → ✅

---

## 2. 基礎設施確認

### WebSocket RPC（免費可用）

| 端點 | 狀態 | 備註 |
|------|------|------|
| `wss://ethereum.publicnode.com` | ✅ | eth_subscribe 可用 |
| `wss://eth.drpc.org` | ✅ | 備用 |
| `wss://mainnet.gateway.tenderly.co` | ✅ | 備用 |

### eth_subscribe 模式

- `newPendingTransactions`（hash only）→ ✅ 每秒 10-20 筆
- `newPendingTransactions, true`（full tx）→ ✅ 包含 input data
- pending tx 大多是 ERC20 transfer，Uniswap swap 約佔 1-2%
- 大型 swap 多走 **private mempool**（Flashbots），公開 mempool 看不到

### eth_getLogs（鏈上確認的 Swap）

更可靠的方式：查 Uniswap V3 Swap event log。

- `WETH/USDC 0.05% pool`：`0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640`
- `WETH/USDT 0.01% pool`：`0x11b815efb8f581194ae79006d24e0d814b7697f6`
- Swap topic：`0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67`

**重要 Bug 記錄**：token0/token1 順序不能假設！

```
WETH/USDT 0.01% pool:
  token0 = WETH (0xc02aaa...) — 18 dec
  token1 = USDT (0xdac17f...) — 6 dec

WETH/USDC 0.05% pool:
  token0 = USDC (0xa0b869...) — 6 dec  ← 注意！USDC address < WETH
  token1 = WETH (0xc02aaa...) — 18 dec
```

直接當 6 dec 解析 WETH amount → 得到天文數字（$4.8 兆）。

---

## 3. 大 Swap 頻率實測

最近 50 blocks（約 10 分鐘，Ethereum 12 sec/block）：

| 規模 | 筆數 | 每 block 頻率 |
|------|------|-------------|
| > $50,000 | 5 筆 | 0.10 筆/block |
| > $30,000 | ~15 筆 | 0.30 筆/block |
| > $10,000 | ~58 筆 | 1.16 筆/block |

**實際觀測到的大 swap（最近 100 blocks）：**

| USD 規模 | 方向 | pool |
|---------|------|------|
| $222,694 | USDC→WETH | 0.05% |
| $92,662 | WETH→USDC | 0.05% |
| $84,976 | USDC→WETH | 0.05% |
| $79,164 | WETH→USDC | 0.05% |
| ... | ... | ... |

→ **每個 block 平均有 1 筆 $10k+ swap，每 10 分鐘有 5 筆 $50k+ swap。**

---

## 4. Backrun 機會確認

在大 swap 後立刻呼叫 Quoter，確認最佳路徑 net_real：

| Q | net_real（現在） | 與 Day 18 相比 |
|---|----------------|--------------|
| $100 | -$0.107 | -$0.086（Day 18）|
| $500 | -$0.837 | -$0.736（Day 18）|
| $1000 | -$2.425 | -$2.199（Day 18）|

**結論：即使在 $222k swap 後立刻查 Quoter，net_real 仍為負。**

原因：Quoter 查的是「現在 latest block」的狀態，
而 latest block 已包含 MEV bot 的 backrun tx（他們先搶走了）。

要看到「swap 後但 backrun 前」的池子狀態，需要：
1. **Archive node + eth_call with block override**（模擬 swap 後的狀態）
2. 或接 **Flashbots MEV-Share orderflow**（看到 pending swap 時就計算）

---

## 5. 今日最重要的學習

> **我們的 Quoter 永遠查不到「機會存在的那個瞬間」。**
>
> 因為 Quoter 看的是「現在 latest block 的池子狀態」，
> 而 latest block 已經是 MEV 清場後的結果。
>
> 真正的 backrun bot 是這樣工作的：
> 1. 從 Flashbots MEV-Share 拿到 pending swap tx
> 2. 在本地模擬：「如果這個 swap 先執行，池子狀態會是什麼？」
> 3. 計算 backrun net，如果正 → 立刻打包 bundle 送出
>
> 我們用的是公開 RPC，永遠比 Flashbots 慢。

### 要完全進入這個賽道需要：

| 需求 | 說明 | 成本 |
|------|------|------|
| Flashbots MEV-Share | 拿到 pending tx orderflow | 免費（但要設定） |
| Archive node 或 local simulation | 模擬 swap 後狀態 | $50-200/月 |
| Bundle 送出 | Flashbots Bundle API | 免費（按 MEV 分配） |
| 智能合約 | 原子 backrun 合約 | 需要部署 |

---

## 6. 21 天計畫回顧（Day 19/21）

| 里程碑 | 狀態 |
|--------|------|
| EV 模型（AMM + MEV）| ✅ Day 1-9 |
| 真實數據掃描器 | ✅ Day 10-14 |
| Gas 動態計算 | ✅ Day 15 |
| v3 tick 幻覺確認 | ✅ Day 16-17 |
| 最佳路徑定位 | ✅ Day 18 |
| Mempool 接通 | ✅ Day 19 |
| 真實 backrun 機會 | ❌ 需要 Flashbots 接入 |
| 真實交易執行 | ❌ 待 Day 20-21 |

---

## 7. Day 20 計畫（倒數 2 天）

- [ ] **P0**：接 Flashbots MEV-Share（免費 API，看 pending swap hint）
- [ ] **P1**：用 web3.py eth_call override 模擬 swap 後狀態
- [ ] **P2**：量化 Day 1-19 的學習成果，寫 21 天總結草稿
