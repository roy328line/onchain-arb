# Day 18 — 費率考古：找到最佳路徑，釐清 backrun 是唯一出路

日期：2026-08-23

---

## 1. 今日工作

**P0**：系統性掃描不同 fee tier 組合，找出費率最低的三角路徑
**P1**：測試 LINK/AAVE/CRV/stETH 等非主流 token 路徑
**結論**：明確定位「最佳可行路徑」與觸發條件

---

## 2. Fee Pool 生態圖（Ethereum Mainnet 實測）

| Pool | Fee Tier | 流動性狀況 |
|------|---------|-----------|
| WETH/USDC | 0.01% | ✅ 深度佳（2391 USDC/WETH 正確） |
| WETH/USDT | 0.01% | ✅ 深度佳 |
| WETH/DAI  | 0.01% | ❌ 幾乎無流動性（報 0.84 DAI/WETH，嚴重偏離） |
| DAI/USDC  | 0.01% | ✅ 存在（1000 DAI → 999.97 USDC） |
| DAI/USDT  | 0.01% | ✅ 存在 |
| WETH/USDC | 0.05% | ✅ 深度最佳 |
| LINK/WETH | 0.05% | ✅ 存在（rate 正確） |
| AAVE/WETH | 任何   | ❌ Quoter 失敗（pool 不存在或無流動性） |

---

## 3. 全路徑費率掃描結果

### 最佳路徑排名（Q=$100，不含 gas）

| 排名 | 路徑 | Fee 組合 | net_real | ROI |
|------|------|---------|---------|-----|
| 1 | DAI→USDT→WETH→DAI | 0.01%+0.01%+0.05% | -$0.073 | -0.073% |
| 2 | DAI→USDC→WETH→DAI | 0.01%+0.01%+0.05% | -$0.082 | -0.082% |
| 3 | DAI→USDC→WETH→DAI | 0.01%+0.05%+0.05% | -$0.060 | -0.060% |
| 4 | 主流路徑（DAI→WETH→USDT）| 0.05%+0.05%+0.05% | -$0.90  | -0.90% |

**最佳路徑：`DAI→USDT(0.01%)→WETH(0.01%)→DAI(0.05%)`**

Q=10 的精細掃描：

| Q | net | ROI | 與 gas 的差距 |
|---|-----|-----|-------------|
| $10 | -$0.007 | -0.073% | gas=$0.09，差 $0.097 |
| $100 | -$0.086 | -0.086% | 差 $0.176 |
| $500 | -$0.736 | -0.147% | 差 $0.826 |

### Stable-Stable 三角（DAI/USDC/USDT）

| 路徑 | Q=$1000 | ROI |
|------|---------|-----|
| DAI→USDC→USDT→DAI（全 0.01%）| -$0.28 | -0.028% |
| DAI→USDT→USDC→DAI（全 0.01%）| -$0.32 | -0.032% |

ROI 極小，但 **Q 必須很大（$50,000+）才能讓 net 超過 gas**，而那時 slippage 已急劇增大。

---

## 4. 核心結論：需要多少偏離才能觸發機會？

**最佳路徑 Q=$100 的 break-even 計算：**

```
需要 net > gas
目前 net = -$0.086
gas = $0.09
缺口 = $0.176

觸發條件：池子相對均衡價格偏離 ≥ 0.176 / 100 = 0.176%
```

**0.176% 的偏離在什麼時候發生？**
→ 當一筆大 swap（$50,000+）打進 WETH/USDT 或 WETH/USDC pool 的時候

---

## 5. 戰略轉向：靜態掃描 → Backrun 策略

### 為什麼靜態掃描找不到機會

目前的 `tri_scanner.py` 是**靜態掃描**：
- 每個 block 看一次池子狀態
- 算 net_real → 全部為負
- 因為 MEV bot 在「有機會的那個 block」裡直接套走了

### Backrun 是什麼

```
Block N：大 whale 把 $100,000 USDT swap 成 WETH
  → WETH/USDT pool 價格偏離均衡 ~0.5%
  → 三角套利機會出現（net > 0）

Block N 內（下一個 tx slot）：MEV bot 送出 backrun tx
  → 套走所有套利利潤

vs.

Flashbots bundle：我的 backrun tx 和 whale tx 打包在同一個 block
  → 比競爭者快 1 個 block
```

### Day 19-20 計畫：接 mempool 監控

```python
# 偽代碼：backrun 策略框架
async def watch_mempool():
    async for tx in pending_txs:
        if is_large_swap(tx, min_usd=50_000):
            path = find_affected_pool(tx)
            if path:
                net = simulate_after_swap(path, tx)
                if net > gas_cost:
                    send_bundle(backrun_tx, tx)
```

**需要的基礎設施：**
1. Flashbots Protect RPC 或 MEV-Share
2. Pending tx 監聽（eth_subscribe pendingTransactions）
3. simulate_after_swap（在 tx 執行後模擬池子狀態）

---

## 6. 今日最重要的學習

> **Ethereum mainnet 的套利機會不在「靜態均衡」裡，在「動態偏離」裡。**
>
> 每個 block 結束時，池子都是相對均衡的（MEV 已清場）。
> 機會出現在大 swap 的「之後這個 block」的剩餘空間。
>
> 這就是為什麼三角套利幾乎都是 backrun，而不是純粹的掃描型套利。
> 我們過去 17 天建的模型是對的，但時機假設是錯的：
> 機會不在「現在」，在「下一個大 swap 之後的毫秒」。

---

## 7. Day 19 計畫（倒數 3 天）

- [ ] **P0**：研究 Flashbots MEV-Share / eth_subscribe pendingTransactions
  - 確認是否能在免費 tier 接到 pending tx
  - 找到 WETH/USDT 0.01% pool 的大 swap（$50,000+）
- [ ] **P1**：建 backrun_detector.py
  - 監聽 pending tx，過濾大 swap
  - 模擬 swap 後的池子狀態
  - 計算 backrun net_real
- [ ] **P2**：如果 P0/P1 可行，建立完整的 backrun bundle 框架
