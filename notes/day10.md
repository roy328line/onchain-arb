# Day 10 — eth_call 驗證：機會是真實的嗎？

> 2026-08-15（Day 10 / 21）

---

## 核心問題

Day 9 找到 profit_pct=4.7% 的候選：
`WETH→USDC→USDT→WETH`（v2+v2+v3），net=$1172（掃描器估算）。
今天用 `eth_call` 直接打鏈上，確認真實報價。

---

## 驗證結果

### ① v2 兩腿：`getAmountsOut`（Router 0x7a250d）

| 項目 | 掃描器估算 | 鏈上真實 | 誤差 |
|------|------------|----------|------|
| Q*   | 13.13 WETH | 13.13 WETH | — |
| USDC out | 24,573.46 | 24,534.17 | **0.16%** ✅ |
| USDT out | 24,210.38 | 24,119.46 | **0.38%** ✅ |

→ v2 兩腿模型精度完全可信，誤差 < 0.4%（來自 block 推進）

---

### ② v3 CA 腿：`Quoter.quoteExactInputSingle`（USDT → WETH）

| 項目 | 掃描器估算 | Quoter 真實 | 誤差 |
|------|------------|-------------|------|
| USDT in | 24,210 | 24,119（真實 v2 輸出）| — |
| WETH out | 13.751 | **12.783** | **+7.6%** ❌ |

→ v3 Quoter 顯示：掃描器的 CA 腿高估了 WETH 輸出 **0.968 WETH = $1,840**

---

### ③ 端對端 Q 掃描（真實報價）

```
Q=  1 WETH  net=-0.0106 WETH  = -$20
Q=  5 WETH  net=-0.0795 WETH  = -$151
Q= 13 WETH  net=-0.3413 WETH  = -$648
Q= 30 WETH  net=-1.4260 WETH  = -$2709
```

**所有 Q 值均為負 EV。這條路徑不存在真實套利機會。**

---

## 根本原因

```
池 0x4e68ccd3（Uniswap v3, USDC/WETH, fee=0.3%）：
  掃描器虛擬儲備：r0=835,293 USDC, r1=489.62 WETH
  implied WETH price：835293 / 490 = 1,706 USDC/WETH（市場 ≈ 1,900）

  掃描器估算：24,210 USDT → 13.751 WETH（implied 1761 USDT/WETH）
  Quoter 真實：24,119 USDT → 12.783 WETH（implied 1887 USDT/WETH）
```

掃描器高估 WETH 輸出的原因：v3 concentrated liquidity 的虛擬儲備（sqrtP × L）
反映的是**當前 tick 附近的流動性深度**，但這個深度無法直接代入 xy=k 公式。
真實 swap 路過多個 tick，每個 tick 有不同的 L，Quoter 內部正確做了這件事。

**implied_ratio=0.90 通過了我們的過濾，但仍是幻覺。**

---

## 今天學到的核心結論

> **v3 虛擬儲備 + AMM 公式 = 系統性高估輸出。**
>
> 不論過濾條件設得多細（implied_ratio、reserve_ratio、depth），
> 只要掃描器用的是虛擬儲備，v3 腿的估算就不可信。
> **Quoter 是 v3 唯一可信的報價來源。**

### 量化幻覺大小

| 路徑類型 | 模型估算 | Quoter 真實 | 偏差 |
|----------|----------|-------------|------|
| v2+v2（兩腿 v2）| ±0.4% | 同 | 可信 |
| v2+v2+v3（v3 CA 腿）| +7.6% 高估 | 正確 | **不可信** |

---

## 下一步（Day 11）

把 Quoter 整合進掃描器：

```python
# 替換 simulate_tri_arb 的 v3 腿：
# 用 eth_call → Quoter.quoteExactInputSingle 取代 amm_out(pool_v3, ...)

def v3_quote_onchain(token_in, token_out, fee, amount_in_wei) -> int:
    # eth_call → Quoter → 回傳真實 amount_out（wei）
    ...
```

成本評估：每次掃描要對每個 v3 腿打一次 eth_call。
- 平均 8006 路徑 × 1 v3 腿 ≈ 8000 次 eth_call / 掃描週期
- publicnode 免費層速率限制需要確認

替代方案：**先只掃 v2-only 三角路徑**——v2 完全可信，先確認有沒有真實 v2 機會存在。

---

## 進度盤點（Day 10 / 21）

| 里程碑 | 狀態 |
|--------|------|
| EV 模型 | ✅ |
| 真實資料接入 | ✅ |
| Scanner 骨架 | ✅ |
| Leg 抽象 + bundle venue | ✅ |
| **v2 路徑驗證（eth_call）** | ✅ 誤差 < 0.4% |
| **v3 路徑驗證（Quoter）** | ✅ 高估 7.6%，幻覺確認 |
| **任何機會真的存在過** | ❌ v3 腿幻覺；v2-only 尚未確認 |
