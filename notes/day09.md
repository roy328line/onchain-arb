# Day 9 — 回 Leg 抽象，確認真實機會存在過

> 2026-08-14（Day 9 = 21 天第三週前夕）

---

## 今天做了什麼

### ① 切換到 bundle venue

`tycho_scanner.py` 和 `tri_scanner.py` 均改為：

```python
CHAIN = ChainParams(venue="bundle", base_gas_usd=4.0, priority_fee_usd=1.5)
```

**f_cost=0**（bundle 失敗不上鏈，無 revert gas）。
判斷邏輯簡化：`net_raw > gas_cost → go`，不再依賴 sigmoid bribe 模型。

---

### ② Leg 抽象

在 `models/ev_model.py` 新增 `Leg` dataclass：

```python
@dataclass
class Leg:
    pool_addr:  str
    token_in:   str
    token_out:  str
    amount_in:  float   # token 單位（非 wei）
    amount_out: float   # token 單位（非 wei）
    dex:        str
    fee:        float
```

`scan_triangles()` 輸出的每條 entry 現在帶 `legs: list[Leg]`，
包含每條腿的完整資訊（組裝 Flashbots bundle tx 的前置條件）。

---

### ③ 過濾幻覺機會（今天最大的坑）

**症狀：** 掃描輸出 net=$142,756，profit_pct=1300%——完全不可能。

**根源追蹤過程：**

| 過濾層 | 問題 | 修法 |
|--------|------|------|
| tick_aware 過濾 | v3 sqrtP 接近 tick 邊界 → r0 爆炸（r0=155T） | 已有，但未抓到所有情況 |
| 儲備比率 `r0/r1 > 10000` | 比率 155T/6 = 25T → 抓到 | 新增 `MAX_RESERVE_RATIO=10_000` |
| implied price（穩定幣對） | `0x48da` DAI/USDC pool：r0=407 DAI, r1=157k USDT → 387x → v2 AMM 榨乾淺池 | 新增穩定幣 implied_ratio ∈ [0.5, 2.0] |
| implied price（一般 token 對） | `0x5777` DAI/USDC：implied=5.24（5:1 仍通過 [0.05,20.0]） | 縮到 [0.2, 5.0] |
| 腿深度過濾 | `Q_bc > pool_bc.x * 0.30` → 榨乾淺池 | 新增 `MAX_DEPTH=0.30`（三條腿各自檢查）|

**修後實際效果：**

掃描輸出 profit_pct 從 1300% → 4.7%（前兩名），找到真實候選：

```
WETH→USDC→USDT  profit=4.70%  net=$1172  Q*=13.1 WETH
  AB: uniswap_v2 r0=4674W r1=8.8M USDC (implied=1.01)
  BC: uniswap_v2 r0=1.66M USDC r1=1.67M USDT (implied=1.00)
  CA: uniswap_v3 r0=835k USDC r1=490 WETH (implied=0.90)
```

手算驗算：Q_in=13.13 WETH → W=24573 USDC → V=24210 USDT → Q_out=13.75 WETH，net=0.617 WETH=$1172 ✅

---

## 機會是否「真實存在過」？

**答案是：候選確認，但還沒有最終驗證。**

| 條件 | 狀態 |
|------|------|
| AMM 公式計算正確 | ✅（amm_out 多次驗算） |
| 儲備數字可信（implied_ratio 在範圍內） | ✅（CA 池 implied=0.90，接近 1.0） |
| profit_pct < 5%（邏輯上合理） | ✅（4.70%） |
| net > gas_cost（$5.50） | ✅（$1172 >> $5.50） |
| 這筆交易還沒被搶走（stale 資料？） | ❓ 無法從 Tycho stream 確認 |
| 真實 on-chain 可執行（pool 有足夠深度） | ❓ 需要送出 bundle 才能知道 |

**殘留的幻覺來源：**
v3 concentrated liquidity 的虛擬儲備在當前 sqrtP 可能不反映「可執行深度」。
implied_ratio 在 [0.2, 5.0] 內，不代表池子在所有 tick 都有流動性。

**根本解（Day 10+）：**
用 Tycho Simulation Engine 做模擬替換 amm_out，直接問鏈「這筆交易能成交嗎？」。

---

## 今日技術債

| 債 | 說明 |
|----|------|
| implied_ratio 過濾的 threshold 是拍腦袋 | [0.5,2.0] / [0.2,5.0] 沒有理論依據，需要用 Tycho Simulation 真正驗證 |
| 腿深度 MAX_DEPTH=0.30 和 optimal_size_tri 的 Q_max 重複 | 應該合併 |
| CA 腿的 v3 儲備仍不確定 | 需要 Tycho Simulation 驗證 |

---

## 今天最值得記的一課

> **流動性幻覺有多種形狀：**
>
> 1. 儲備爆炸型（r0/r1 > 10,000x）→ sqrtP 接近 tick 邊界
> 2. 穩定幣失衡型（implied 5:1）→ v3 DAI/USDC 全倒向一側
> 3. 淺池榨乾型（輸入量 > r0 × 30%）→ AMM 允許把池子幾乎清空
> 4. 一般 token 偏離型（implied 10x）→ WETH/UNI 池流動性集中在舊價格帶
>
> **每次新的過濾，都揭露另一種幻覺。** 最終只有 Tycho Simulation
> 能一次解決所有問題——模擬層直接問鏈，不依賴任何「虛擬儲備」假設。

---

## 進度盤點（Day 9 / 21）

| 里程碑 | 狀態 |
|--------|------|
| EV 模型 | ✅ |
| 真實資料接入 | ✅ |
| Scanner 骨架 | ✅ |
| Leg 抽象 | ✅ |
| bundle venue | ✅ |
| **任何機會真的存在過的確認** | ⚠️ 候選找到，最終驗證需要 Simulation |

---

## Day 10 方向

**用 Tycho Simulation Engine 替換 amm_out：**

1. 用 `tycho-client` 的 simulation 介面（或 `eth_call` 直接打合約）
2. 把 Q* 對應的 swap 打出去，看回傳的 amount_out 是否匹配我們的計算
3. 如果不匹配 → 證明 v3 虛擬儲備有問題
4. 如果匹配 → 候選升格為「真實機會曾存在」

替代方案（如果 Simulation 太難）：
- 用 `eth_call` 打 `getAmountsOut(amountIn, [tokenA, tokenB, tokenC])` 到 Uniswap v2 Router
- v2 路徑完全可信，先把 v2-only 路徑做完整端對端
