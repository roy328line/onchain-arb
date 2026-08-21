# Day 16 — Gas 降低無法救 net_real；診斷模型高估根因

日期：2026-08-21

---

## 1. 掃描結果（20 分鐘 / 96 blocks / Arbitrum）

| 指標 | 數值 |
|------|------|
| 掃描時長 | 1203s / 96 blocks |
| 追蹤池數 | 3,049 |
| 三角路徑數 | 7,984 |
| net_star_usd > gas_cost 次數 | 0（gas_cost 顯示 $5.50 — 舊掃描，見 §2） |
| 最佳 net_real_usd（Quoter 後） | **-$1.047**（DAI→WETH→USDT） |

掃描輸出每行顯示 `Gas 門檻 : net_star_usd > $5.50` ——
這是因為背景進程是之前啟動的，**動態 gas 已在 Day 15 寫入但此次掃描用的是舊的啟動參數**。
新啟動的 scanner 正確讀到 `dynamic gas = $0.0909`。

---

## 2. Gas 降低能救多少？（重要結論）

| gas_cost 門檻 | net_real > 門檻 筆數（2,638 筆 Quoter）|
|--------------|--------------------------------------|
| $5.50（舊）  | 0 |
| $0.09（新）  | **0** |
| $0.00        | **0** |

**gas 不是瓶頸。** 所有路徑的 `net_real_usd` 全為負，最佳也只有 -$1.047。
降低 gas 門檻對正 EV 數量毫無影響——問題出在 net_real 本身。

---

## 3. 根本問題：AMM 模型高估利潤 ~70x

| 路徑 | Q_star | net_star_usd | net_real_usd | 高估倍數 |
|------|--------|-------------|-------------|---------|
| DAI→WETH→USDT | $658 | +$71.59 | -$1.05 | ∞（正負反轉） |
| USDC→WBTC→USDT | $11,068 | +$3,938 | -$117 | ∞ |
| USDC→ETH→WBTC | $13,762 | +$2,843 | -$99 | ∞ |

**net_star 是正的，net_real 是負的。** 模型給出「有機會」的信號，
但 Quoter 鏈上報價全部翻轉。這不是邊緣誤差，是系統性高估。

### 可能原因（優先序）

1. **v3 tick 邊界**：`optimal_size_tri` 用 `pool_ab.x * 0.3` 當 Q_max，
   但 v3 流動性是分 tick 集中的，虛擬儲備（`x = L²/price`）高估可動用深度。
   Quoter 只看實際 tick 內的流動性 → 真實 slippage 遠大於模型預測。

2. **Q_star 過大**：最佳路徑 Q_star=$658，
   但 Arbitrum 上 DAI/WETH 的 tick 流動性可能只支撐 $50-100 的 swap 而不大幅偏離。
   模型認為 $658 最優，但鏈上 $658 的 slippage 已把利潤吃光。

3. **三腿累積 slippage**：即使單腿誤差 2%，三腿累積可達 6%，在小機會上足以翻轉正負。

### 確認方式（Day 17 工作）

```python
# 用 Quoter 做小額掃描：從 Q=10 開始，步進找 net_real=0 的臨界點
for q in [10, 50, 100, 200, 400, 600]:
    net_real = quoter_verify_tri(path, Q=q)
    print(f"Q={q}: net_real={net_real:.4f}")
```

如果 Q→0 時 net_real 也是負的，代表路徑根本不存在機會（pool 不對稱方向）。
如果 Q=10 時 net_real > 0，代表需要更小的 Q_star——模型高估可動用深度。

---

## 4. CrossEx Terminal 狀態

`localhost:6688` → Connection refused。Terminal 未啟動。

Day 15 建的 `dry_run_strategy.py` 已就位，但 CrossEx Terminal 本身是
`pendle-finance/arbitrage-with-crossex` 的本地程序，需要：
1. 安裝 Node.js 依賴（`npm install`）
2. 設定 API keys（各 CEX 的 subaccount）
3. 背景啟動（`npm start`）

目前缺少 CEX API keys，**CrossEx 接通延後到取得 keys 再做**。
Day 16 的 `/api/opportunities` 查詢無法執行。

---

## 5. 今日最重要的學習

> **gas 費用是最後一道關卡，不是第一道。**
> 當 net_real 全為負時，從 $5.50 降到 $0.09 毫無意義。
> 真正的問題是：模型計算的「有機會」和鏈上實際回報之間有系統性的 70-100x 差距。
> 
> 要找到真實機會，必須先理解這個 gap 的來源：
> 是 v3 tick 造成的流動性幻覺，還是三腿 slippage 累積，
> 還是 Q_star 估算用了錯誤的池深度假設？

---

## 6. Day 17 計畫

- [ ] **P0**：用 Quoter 做 Q 掃描（10→600）找 net_real=0 的臨界點
  - 若臨界點存在 → 找到真實可交易的 Q，更新 optimal_size_tri
  - 若所有 Q 都是負 → 路徑本身就沒機會，要換路徑
- [ ] **P1**：比較 v2 vs v3 路徑的 net_star/net_real 比值
  - v2 應該更準確（沒有 tick 問題），先把 v2-only 路徑列出來
- [ ] **P2（待 token）**：push 兩個 pending commits 到 GitHub

---

## Pending

- GitHub push 等待新 Personal Access Token（兩個 commit 在本地）
