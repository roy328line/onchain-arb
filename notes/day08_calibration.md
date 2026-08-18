# Day 8 校準筆記

> 2026-08-13

---

## 資料來源

- 方法：直接從公開 RPC（ethereum.publicnode.com）掃 200 個最新 block
- 抓法：Transfer event → bot WETH 淨流入 = gross_profit_lower_bound
- bribe：priority_fee_per_gas × gas_used（**不含 coinbase.transfer，是下限**）
- 樣本：369 筆疑似 atomic arb，過濾後 356 筆有效（0 < bribe_ratio ≤ 1.5）

---

## 校準結果

| 桶 | n | k | midpoint | p50 | p90 |
|----|---|---|----------|-----|-----|
| S (<0.003 ETH gross) | 211 | 3.228 | 0.353 | 0.168 | 0.961 |
| M (0.003-0.015 ETH)  | 17  | 50.0  | 0.016 | 0.012 | 0.117 |
| L (0.015-0.06 ETH)   | 50  | 50.0  | 0.012 | 0.013 | 0.019 |
| XL (>0.06 ETH)       | 78  | 50.0  | 0.001 | 0.000 | 0.005 |

⚠️ **M/L/XL 桶的數字不可信**：p50 ≈ 0 代表這些 bot 主要用 coinbase.transfer 付 bribe，
priority fee 幾乎為 0，我們量到的只是雜訊。

**只有 S 桶的 k=3.228, mid=0.353 有參考價值（但仍是下限）。**

---

## 對 best_ev 的影響

同一組池（USDC/WETH，$11.73 gross profit，gas=$3）：

| 模型 | venue | r* | EV* | 決策 |
|------|-------|----|-----|------|
| 舊猜測 k=5.65, mid=0.95 | public | 1.000 | -$1.93 | no-go |
| 舊猜測 k=5.65, mid=0.95 | bundle | 0.758 | +$0.41 | go |
| S桶下限 k=3.228, mid=0.353 | public | 0.621 | +$0.46 | **go** ← 翻轉 |
| S桶下限 k=3.228, mid=0.353 | bundle | 0.366 | +$2.18 | go |
| 中間估計 k=4.0, mid=0.60 | public | 0.819 | -$0.46 | no-go |
| 中間估計 k=4.0, mid=0.60 | bundle | 0.549 | +$1.36 | go |

**關鍵發現：** 真正的 midpoint 在 0.353（下限）到 0.95（上限）之間。
- 如果 midpoint < ~0.7：public mempool 就有正 EV，不需要等 bundle
- 如果 midpoint > ~0.7：需要 bundle 才有正 EV

---

## 為什麼 M/L/XL 桶都是 0？

大額 arb bot 的 bribe 結構不同：
- **小額**：用 priority fee（快速，不需要複雜合約）
- **大額**：直接在合約裡 `block.coinbase.transfer(bribe)`，priority fee 設很低

這代表我們的估算方法對大額 arb 完全失效。
要校準 M/L/XL 桶，必須解 internal transaction（需要 archive node + trace）。

---

## 校準結論（當結論，不是待辦）

**S 桶樣本的選擇偏誤**來自搜尋者基礎設施水準差異（priority fee bot vs coinbase.transfer bot）。
我們觀測到的是「用 priority fee 競標的小額 bot」，而非全體競爭者。
這讓競爭看起來比實際容易（midpoint 低估）。

**在自己送出 bundle 並觀察中標率之前，本類別的 EV 絕對值不可用於決策，只可用於同類別內的相對排序。**

**M/L/XL 三桶標記 FIT_FAILED**：k=50.0 是最佳化器撞上界，代表 CDF 形狀不是 sigmoid（幾乎全部在 r≈0），不是「競爭激烈」的發現。這三桶的資料無效，原因是大額 bot 用 coinbase.transfer 付 bribe，priority fee ≈ 0。

## Day 9 方向

回到 Leg 抽象，不再繼續 bribe 校準。

原因：midpoint ∈ [0.35, 0.95] 用只看贏家的資料收不緊——再花兩天縮到 [0.5, 0.9]，決策仍會在區間內翻轉。繼續校準的邊際報酬接近零。

Day 9 重點：把掃描器的 Leg 抽象做對，確認真實機會存在過。


1. **真正的 midpoint 是 [0.353, 0.95] 之間的某個值**
2. **不確定性太大，目前不應把 BribeModel 當真**
3. **正確的 bribe 方向**：Flashbots bundle + coinbase.transfer，不是 priority fee
4. **下一步**：把模型換到 bundle venue，去掉 bribe 的不確定性
   - bundle venue 下 f_cost=0，EV 只取決於 gross_profit 和 gas，不再依賴 sigmoid

---

## 實作建議（Day 9）

把掃描器的判斷邏輯改成：

```python
# 不再猜測 bribe sigmoid，改成：
# 1. 只考慮 bundle venue（f_cost=0）
# 2. r* 用解析解：r* = 1 - 1/(k*(1-p_win))
# 3. go 條件：net_raw > gas_cost（根本不用 bribe 模型）
```

bundle venue 下，EV = p_win × (net_raw - gas - bribe) - 0（f_cost=0）
最優 bribe 是讓競標贏，但因為 f_cost=0，就算 bribe=0 也可以試（只是 p_win 低）。
實際操作：先用 0 bribe 確認機會真的存在，再考慮最優 bribe 策略。
