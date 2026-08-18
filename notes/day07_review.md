# Day 7 Review — 修正清單與方法論筆記

> 2026-08-12（Day 7 = 21 天三分之一）

---

## 進度盤點

| 里程碑 | 狀態 |
|--------|------|
| EV 模型 | ✅ |
| 真實資料接入 | ✅ |
| Scanner 骨架 | ✅ |
| **任何機會真的存在過的證據** | ❌ |

所有輸出目前都建立在猜測的 `k=5.65` 和 `midpoint=0.95` 上。  
② 的對照表顯示 `r*` 對 venue 和 k 極度敏感。  
**校準是接下來的唯一重點。**

---

## 今天最值得記的一課

> **註解裡的疑慮就是測試該覆蓋的位置。**
>
> Day 2 我在 docstring 寫「fee 不同時要小心 γ1 vs γ2 的位置」然後猜錯邊；  
> Day 7 我在測試裡寫「B→A 的池組合可能不正確」然後把測試改成繞過它。  
> 兩次都是先察覺到不對勁，然後用文字記錄下來就放過了。  
>
> **以後只要打出「可能」「要小心」「應該沒問題吧」這類字，**  
> **就停下來寫測試——那不是註解，那是 bug 的座標。**

---

## P0 修正清單（已完成）

| 項目 | 問題 | 修法 |
|------|------|------|
| P0-1 | sentinel `return 0.0` 讓優化器永遠收斂到界外 | 改 `return float("inf")` |
| P0-1 | `surplus_for_bribe` key vs 呼叫方讀 `r["surplus"]` | 統一為 `surplus` |
| P0-2 | `net_ab` vs `net_ba` 跨單位比較 | 加 `price_x` 參數，方向比較用 USD |
| P0-3 | verify_all 第3項是假測試（繞過 B→A） | 改成真正示範 `price_x` 影響 `net_star_usd` |
| P0-3 | `test_amm_x_new` 恆真 assert | 刪除 |
| P0-4 | `Q_max = min(pool_ab.x, pool_bc.x, pool_ca.x)` 跨 token | 只用 `pool_ab.x` |
| P0-5 | dedup `sorted()` 讓反向路徑消失 | 改旋轉正規化，保留兩個方向 |

---

## r* 行為分析（Day 7 ② 驗證結果）

### 同一組池：venue=public vs bundle

```
venue   | Q*    | r*     | EV*      | 決策
public  | 5,499 | 1.0000 | −$1.93   | no-go
bundle  | 5,931 | 0.7580 | +$0.41   | go    ← 一批 no-go 在這裡翻轉
```

**真正的機制（不是「減少損失」）：**  
surplus = $4.73，輸掉拍賣要付 f_cost = $4.5。  
「獎品 < 罰款」時，把全部利潤送給 builder 換取「不要輸」才是最優策略。  
這是用利潤買保險，不是減少 net_after 的損失。

**閉式估計（bundle）：**  
`dEV/dr = 0 → (1−r) = 1/(k·(1−p_win)) → r* ≈ 0.762`（與數值解 0.758 吻合）

**⚠️ 以上數字建立在猜測 sigmoid 上，Day 8 校準後會變。**

### r* 三種情境的方向（verify_all 第 8 項）

| 情境 | r* | 機制 |
|------|-----|------|
| public + surplus > 0 + f_cost 大 | → 1.0 | 用利潤買保險 |
| bundle（f_cost=0） | 內部解 ~0.76 | 真正的利潤最大化 |
| 真虧損（net_raw < gas） | → 0.0 | 寧可輸掉，不要贏到虧損 |

---

## P1 修正清單（已完成）

| 項目 | 問題 | 修法 |
|------|------|------|
| P1-1 | tycho_scanner v3 fee 單位錯（bps 非 pips） | `"v3" in dex` → `/1_000_000` |
| P1-1 | update_state 只讀 reserve0/reserve1，v3 靜默不進 reg.pools | 加 v3 分支：sqrtP+L → 虛擬儲備 |
| 技術債 | PoolRegistry/hex_to_int/decimals 表與 tri_scanner.py 重複 | **暫不 refactor**，頂端加警告註解 |

---

## Day 8 方法論預警：從贏家資料估 p_win

### 致命的選擇偏誤

`roadmap` 原本寫：用 Flashbots bundle 歷史資料做 logistic 回歸校準 `k / midpoint`。

**問題：鏈上只看得到贏家。**

- 觀察到的是 `P(bribe_ratio | 這筆贏了)`
- 需要的是 `P(贏 | bribe_ratio)`
- 沒有輸掉的 bundle 資料 → logistic 回歸所有樣本 `y=1` → 估不出來

### 正確做法：從得標價估競爭者出價分布

```
p_win(r) = P(我的 r > 所有競爭者的最高 r)
          = F(r)，其中 F 是「得標 bribe_ratio」的經驗 CDF
```

得標的 `bribe_ratio` 本身就是「競爭者最高出價」的抽樣。  
只用贏家資料**可以**估出 F——這是拍賣理論從得標價回推競爭強度的標準做法。

### 實際步驟

1. 從 Dune（`hildobby/atomic-mev` 或 Flashbots dashboards）撈歷史 atomic arb
2. 每筆算：`gross_profit`（從 trace 還原）與 `coinbase transfer + priority fee`（= bribe）
3. `bribe_ratio = bribe / (gross_profit − gas)`
4. 按機會規模分桶（$0-10 / $10-50 / $50-200 / $200+）各自畫 CDF
   - 不同規模競爭強度不同，混在一起得到「平均值的幻覺」
5. 用 CDF 直接取代 sigmoid，或用 sigmoid 擬合 CDF（此時 `k` 和 `midpoint` 才有意義）

### Day 8 第一步（今天先確認）

**先確認能不能拿到 `gross_profit`。**  
從 Dune 撈 20 筆真實 atomic arb，看能不能還原「毛利」和「bribe」兩個數字。  
如果撈不到，整個 Day 8 計畫要改——早點知道比較好。

---

## 技術債清單

| 債 | 說明 | 預計處理時機 |
|----|------|------------|
| PoolRegistry 重複 | tycho_scanner / tri_scanner 各有一份 | 第三次踩到同一個坑再抽 |
| v3 虛擬儲備精度 | 忽略 tick range，接近邊界時高估數億倍 | Day 8+ 換 Tycho Simulation |
| 四個 token 字典 | tri_scanner 的 TOKEN_PRICES/DECIMALS/USD_PRICE/SHORT 內容重疊 | 下次整理時合併 |
| MIN_Q_STAR / MIN_POOL_TVL | 暫停——單位系統修對之前不加過濾 | P0 全修完後 |
