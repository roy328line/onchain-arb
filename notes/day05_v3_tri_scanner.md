# Day 05 學習筆記（2026-08-09）

## 今日目標

① v3 池接入 tri_scanner（核心任務）

---

## 完成項目

### Uniswap v3 池接入 `tri_scanner.py`

**三個改動點：**

#### 1. `register_component` — fee 換算邏輯分叉

v2 和 v3 的 fee 單位不同：

| 協議 | 單位 | 換算 | 範例 |
|------|------|------|------|
| Uniswap v2 / Sushi | bps（/10,000） | `fee_int / 10_000` | 30 → 0.3% |
| Uniswap v3 | pips（/1,000,000） | `fee_int / 1_000_000` | 500 → 0.05% |

v3 fee tier：`500(5bps) / 3000(30bps) / 10000(100bps)`

#### 2. `update_state` — v3 虛擬儲備推算

v3 沒有 `reserve0/1`，改從 `sqrt_price_x96 + liquidity` 推算：

```
sqrtP = sqrt_price_x96 / 2^96

virtual_r0 = liquidity / sqrtP    （token0，除以 10^d0 換成人類單位）
virtual_r1 = liquidity * sqrtP    （token1，除以 10^d1）
```

**近似條件：** 在 current tick 附近、Q << pool depth 時，v3 的集中流動性可近似為 x·y=k，誤差 < 1%，足夠快篩用。

#### 3. cmd 加 `--exchange uniswap_v3`

---

## 實測結果

```
池數：3,043（v2: 2,098 + v3: 945 新增）
三角路徑：7,990 條（Day 4 的 92 條 → 86x 成長）
net_star_usd > $5.5：19 個機會 / block
```

### 掃描到的「機會」解讀

**Group 1 — WETH→USDC→0x2ebd→WETH，net ≈ $61,000**

```
Q* ≈ 49~50 WETH（≈ $95,000）
fee = 5+30+100 bps（三腿合計 135 bps）
中間 token = 0x2ebd...（不知名長尾 token）
```

**結論：流動性幻覺。** Q* 遠超池的實際深度，真實 slippage 比模型預測高出數量級。模型在小池上的近似不成立。

**Group 2 — WETH→0xe76c→USDT→WETH，net ≈ $3,000-4,000**

```
Q* = 3~6 WETH（⚠️ Q*<10）
fee = 30+100+5 bps
```

同樣是流動性幻覺，100 bps 費率 + Q* 極小是雙重警訊。

---

## 踩坑記錄

**坑：** v3 fee 如果也用 `/10,000` 換算：
```
500 / 10,000 = 0.05 = 5%（錯！）
500 / 1,000,000 = 0.0005 = 0.05%（正確）
```
會讓所有 v3 池的費率被高估 100x，完全過濾掉 v3 的套利機會。

---

## 發現：機會都是流動性幻覺

**根本原因：** `optimal_size_tri` 沒有對 Q* 做上界約束（pool depth 限制），在極小流動性的長尾 token 池上，模型會算出荒謬的正 net。

**解法（Day 6 實作）：**
```python
MIN_Q_STAR   = 50        # Q* 太小 → 池太淺，排除
MIN_POOL_TVL = 100_000   # 三條腿每個池的 TVL > $10萬（需要接 TVL 資料）
```

---

## 檔案變更

| 檔案 | 變更 |
|------|------|
| `scripts/tri_scanner.py` | v3 fee 換算、虛擬儲備推算、加 uniswap_v3 exchange |

---

*建立於 Day 05 (2026-08-09)*
