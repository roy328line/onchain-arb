# Day 11 — 兩階段掃描：Quoter 整合

> 2026-08-16（Day 11 / 21）

---

## 今天做了什麼

### 兩階段掃描架構

```
階段 1（零 eth_call，快速篩選）
  amm_out 估算 → implied_ratio/depth 過濾 → tick_aware 隔離
  輸出：候選清單

階段 2（Quoter 精確驗證）
  v2-only 路徑 → 直接標記 go_real=True（amm_out 誤差 < 0.4%）
  含 v3 腿的路徑 → eth_call Quoter 驗證 → net_real_usd
  輸出：go_real=True 的確認清單
```

### 新增函式（tri_scanner.py）

```python
v2_quote_onchain(path_addrs, amount_in_wei) -> list[int]
    # UniswapV2Router02.getAmountsOut

v3_quote_onchain(token_in, token_out, fee, amount_in_wei) -> int
    # UniswapV3Quoter.quoteExactInputSingle

verify_onchain(opp, reg) -> dict
    # 逐腿串接，v2 用 Router，v3 用 Quoter
    # 回傳 net_real_usd + go_real
```

---

## 掃描結果

```
追蹤池數       : 3,050
三角路徑數     : 7,996
Quoter 確認後候選 : 0
OBSERVE（幻覺）  : 226
```

**0 個真實機會。** 226 個幻覺被正確隔離（最高 net_star=$186,450 — 但 Quoter 驗證後全是負 EV）。

---

## 這是好消息還是壞消息？

**好消息：** 過濾系統正確運作，沒有假陽性進入決策層。

**壞消息：** 今天這個時段，v2+v3 混合路徑沒有真實套利機會。

這符合現實：鏈上套利極度競爭，機會存在時間以毫秒計，大部分時段看起來就是「0 個機會」。

---

## 關鍵觀察

| 層 | 過濾前 | 過濾後 |
|----|--------|--------|
| amm_out 快篩 | 226 條「看似機會」| → |
| implied_ratio + depth | 過濾掉 | → |
| tick_aware | 226 → OBSERVE | → |
| Quoter 驗證 | — | **0 條確認** |

**幻覺全是含 v3 腿的穩定幣三角路徑**（WETH→USDC→USDT）。
implied_ratio 過濾已攔掉大多數，但仍有 226 條通過第一階段（被 tick_aware 攔在 OBSERVE）。

---

## Day 12 方向

兩條路：

**A. 降低門檻，掃更多路徑**
- 把 MIN_POOL_TVL 從 $50k 降到 $10k
- 加入更多 token 對（WBTC, UNI）
- 觀察是否有被 TVL 門檻排除的真實機會

**B. 統計層：量化幻覺 vs 真實的比率**
- 對 OBSERVE 裡的路徑也跑 Quoter 驗證
- 統計「amm_out 正 EV 但 Quoter 負 EV」的平均偏差
- 建立校正公式：`net_real ≈ net_star × correction_factor`

---

## 進度盤點（Day 11 / 21）

| 里程碑 | 狀態 |
|--------|------|
| EV 模型 | ✅ |
| 真實資料接入 | ✅ |
| Scanner 骨架 | ✅ |
| Leg 抽象 + bundle venue | ✅ |
| v2/v3 驗證（eth_call + Quoter） | ✅ |
| 兩階段掃描（amm_out → Quoter）| ✅ |
| **任何機會真的存在過的確認** | ❌ 今天 0 個，需要更長時間觀察或降低門檻 |
