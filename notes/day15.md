# Day 15：CrossEx API 接入 + Gas 動態化

## 昨天待辦完成

**Boros fee 確認（官方文件）：**
- **Trade Fee**：`notional × 0.05% × TTM_years`（按到期時間比例，非一次性全額）
- **Settlement Fee**：`notional × 0.2%/yr × period_years`，兩腿都付
- `SETTLEMENT_FEE_BASIS = "CONFIRMED_ANNUAL_PRORATED"`
- 正確 APR（maker）：**+18.6%**（修正前 33.3% 高估，P0-3 修正方向錯了一次）

---

## A 線：CrossEx Terminal API 調查

### 關鍵發現
- **開源 Repo**：https://github.com/pendle-finance/arbitrage-with-crossex（MIT）
- **本地 Fastify**：`http://localhost:6688/api`，auth = `x-arb-token: <file>`
- **完全可程式化**：perp 腿可透過 REST API 執行

### 架構限制（重要）
CrossEx Terminal **只執行 Perp 腿**（Leg1 SHORT + Leg2 LONG）。
Boros YU 腿（Leg3 + Leg4）必須在 `app.boros.finance` UI 手動下單。

### 關鍵 endpoint
```
GET  /api/opportunities?notionalUsd=N  # 查當前機會 + APR
POST /api/preview                       # 零副作用驗證
POST /api/deals                         # 執行（需 Roy 確認）
GET  /api/deals/:id                     # 監控
POST /api/deals/:id/stop                # 平倉
```

### `scripts/dry_run_strategy.py`
輸入：EV 模型參數（notional, 利率, TTM）
輸出：
1. EV 摘要（毛利、費用、APR）
2. Boros YU 腿的人工操作指引
3. curl preview 指令（可直接貼）
4. curl execute 指令（注解，需 Roy 取消注解後執行）

---

## B 線：Gas 動態化

### 重大發現：gas_cost 不是 $5.50

Dencun 升級（2024-03-13）後，Ethereum basefee 結構性下降：

| 時間 | basefee | % < 3 gwei |
|------|---------|------------|
| 2023 | 20-80 gwei | 0% |
| 2024-06 | 3 gwei | 21% |
| 2025-02 ~ now | 0.05-0.1 gwei | **100%** |

**今天（2026-08-20）basefee = 0.058 gwei：**

```
gas_cost = 0.158 gwei × 210,000 × $2,500 × 1e-9 = $0.083
```

**不是 $5.50！相差 66 倍。**

DAI→WETH→USDT 的 net_real = -$1.05，break-even gas = $1.05。
現在 gas_cost = $0.083 → **net_ev = -$1.05 + ($5.50 - $0.083) = +$4.37 ✅**

### `scripts/gas_monitor.py`
- `get_gas_cost_usd()` → real-time basefee → GasInfo
- `is_below_break` → True 代表三角套利理論上正 EV
- `tri_scanner.py` 改用 `_get_dynamic_gas_cost()`（失敗 fallback $5.5）

### 敏感度（ETH 不同價格）

| ETH 價格 | gas_cost | break-even gwei | 狀態 |
|---------|---------|----------------|------|
| $1,500 | $0.049 | 3.33 gwei | ✅ |
| $2,500 | $0.083 | 2.00 gwei | ✅ |
| $4,000 | $0.130 | 1.25 gwei | ✅ |

**所有情境下 basefee = 0.058 gwei 都遠低於 break-even。**

---

## 今日結論

1. **三角套利在現在的 gas 條件下理論上正 EV**（net_ev ≈ +$4.37）
2. **Day 13/14 的結論是基於錯誤的 gas 假設（$5.50 是 2023 的水準）**
3. **CrossEx perp 腿可完全程式化**，Boros YU 腿仍需 UI
4. 下一步：跑一次真實掃描，用 dynamic gas_cost 看有多少正 EV 機會

---

## Day 16 計畫

**A**：用新 gas_cost ($0.08) 跑 20 分鐘掃描，統計正 EV 機會數量
**B**：接 `GET /api/opportunities` 查當前 CrossEx 上的即時機會
**C**：把 dry_run_strategy 的 EV 數字接上 ev_model 的精確計算（現在用近似值）
