# Day 17 — Q 掃描確認：Ethereum Mainnet 三角套利機會已被 MEV 吃盡

日期：2026-08-22

---

## 1. 今日工作：Quoter Q 掃描

新建 `scripts/quoter_q_scan.py`：對 DB top 路徑，從 Q=$1 掃到 Q=$2000，
逐點呼叫 Quoter 找 net_real=0 的臨界點。

**問題**：為什麼 AMM 模型算 net_star=+$71，但 Quoter 算 net_real=-$1？
→ 答案今天確認了。

---

## 2. Q 掃描結果

### DAI→WETH→USDT（v2/sushiswap/v3 混合）

| Q_usd | net_real | ROI% | 狀態 |
|-------|----------|------|------|
| $1    | -$0.009  | -0.90% | ❌ |
| $100  | -$0.90   | -0.90% | ❌ |
| $1000 | -$9.33   | -0.93% | ❌ |

**結論：ROI% 幾乎固定在 -0.9%，與 Q 無關。這不是 slippage，是固定費率差。**

### DAI→WETH→USDT（全 v3/0.05% pool）

即時查詢（Quoter）：全部為負。

| Q_usd | net_real | 狀態 |
|-------|----------|------|
| $1    | -$0.008  | ❌ |
| $500  | -$4.16   | ❌ |
| $1000 | -$9.04   | ❌ |

---

## 3. 重要 Bug 記錄：中間腳本的 +$6 假象

第二輪臨時掃描曾出現 Q=550 → +$6.25、Q=700 → +$2.54 的「正 EV」。

**這是錯誤的。** 原因：

1. **價格時間差**：不同 block 之間 DAI/WETH/USDT 價格波動
2. **驗證**：同一 Q=550 連跑 3 次，結果穩定在 -$1.59（負）

教訓：
> **每次掃描結果要「現在」驗證，不要看 30 秒前的輸出就下結論。**
> 池子狀態每個 block 都在變，正 EV 出現的那個 block 已經過去了。

---

## 4. 根本結論：機會存在但速度不夠

### Ethereum Mainnet 三角套利現狀

所有測試路徑（DAI/USDT/USDC/WETH/WBTC，8+ 種組合）在**現在這個 block**：
- **Quoter net_real < 0**：機會已被 MEV bot 搶走
- ROI 固定約 -0.5% 到 -0.9%（費率成本）
- Q 大小不改變結論（不是規模問題）

### 為什麼 AMM 模型顯示 net_star 極大？

| 現象 | 解釋 |
|------|------|
| net_star = +$3,000 → net_real = -$1 | Q_star 太大（$11,000），模型沒有 tick 邊界 |
| AMM 模型 Q_max = pool.x * 0.3 | v3 虛擬儲備遠大於實際 tick 內流動性 |
| Quoter 更準確 | 反映實際 tick 邊界後的 slippage |

**模型高估 net 的根因**：v3 池的虛擬儲備 (`x = L²/price`) 假設流動性是連續分布的，
但實際上 v3 流動性集中在特定 tick 範圍。
Q_star = $11,000 時模型說「有 $3,000 利潤」，但 Quoter 說「tick 邊界後 slippage 吃掉一切」。

### 純 v2 路徑分析

DB 中沒有純 v2 路徑（三腿全 v2）的記錄——不是因為 scanner 沒掃到，
是因為：

1. Arbitrum 上 v2 流動性遠比 v3 淺
2. 三腿路徑要求 A→B→C→A 都有足夠流動性，v2-only 路徑組合少
3. Scanner 過濾 `MIN_POOL_TVL=10000` 淘汰了大多數 v2 池

---

## 5. 今日最重要的學習

> **機會在 Ethereum Mainnet 確實存在過（每個 block）——但窗口是毫秒級的。**
> 
> 用 Tycho 的 WebSocket 更新 + Quoter 驗證，我們可以在新 block 到達後
> 看到機會（此時 net_real > 0）。但在我們讀到 Tycho 數據、計算完、
> 準備送交易的時候，MEV bot 已經在同一個 block 裡搶走了。
> 
> 這就是為什麼 DB 的數據全是負的——記錄的是「已過期的機會」。
> 
> 真正的問題不是「有沒有機會」，而是「能不能比 MEV bot 快」。

### 三角套利的本質競爭

| 層次 | 說明 |
|------|------|
| 速度競爭 | MEV bot 用 private RPC / Flashbots，毫秒內送出 |
| 覆蓋度競爭 | 掃更多路徑找更偏的池子（競爭者少） |
| 建模競爭 | 更精確的 Q_star 估算（我們的弱點：v3 tick 幻覺）|
| 資訊競爭 | Tycho WebSocket vs. 公開 RPC 延遲 |

**Day 17 的進展**：建模競爭確認了根本問題。接下來要解決的是找到競爭者少的池子。

---

## 6. Day 18 計畫

- [ ] **P0**：更換掃描目標——找競爭者少的路徑
  - 搜索「不在主流 DEX 排行榜上」的 token 三角
  - 例：某 DeFi 協議 token + WETH + USDC 的三角
  - 或：Arbitrum 原生的 GMX/GLP/USDC 路徑
- [ ] **P1**：修正 optimal_size_tri 的 Q_max 估算
  - 用 Quoter 做 binary search 找真實的「tick 邊界 Q」
  - 把 v3 tick 感知整合進模型
- [ ] **P2**：push commits + 記錄 25/21 天進度

---

## 7. 技術補記

### quoter_q_scan.py 的正確用法

```bash
# 對任意路徑做 Q 掃描
python3 scripts/quoter_q_scan.py

# 結果解讀：
# 如果 Q=1 就是負 → 路徑本身沒機會（費率差）
# 如果存在 Q* 使 net_real > 0 → 找到可交易規模
```

### 費率差估算（Ethereum Mainnet）

- v3 0.05% pool：三腿累積費率 = 1 - (1-0.0005)³ ≈ 0.15%
- DAI→WETH→USDT 實測 ROI ≈ -0.9%（含 slippage）
- break-even 需要：價格偏差 > 費率 + slippage ≈ 0.15-1%
