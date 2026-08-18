# 21 天鏈上套利共學 — 進度總覽（更新於 Day 05）

> 2026-08-05 ～ 2026-08-26

---

## 已完成

| Day | 日期 | 主題 | 關鍵產出 |
|-----|------|------|---------|
| 01 | 08-05 | EV 成本模型 | `ev_model.py`：AMM 模擬、optimal_size、best_ev、verify_all() 七項通過 |
| 02 | 08-06 | AMM / Mempool / LiFi | price impact curve、mempool 觀察（11,020 tx/10min）、LiFi API 實測 |
| 03 | 08-07 | 真實池數據接入 | `fetch_pools.py`（eth_call getReserves，同 block batch）、`scanner.py`（go/no-go）、3 個 bug 修正 |
| 04 | 08-08 | 全池掃描 + 三角套利 | `tycho_scanner.py`（2097池/22幣對）、`tri_scanner.py`（92條路徑，v2費用牆=90bps） |
| 05 | 08-09 | v3 池接入 | tri_scanner 加 v3（945池/7990路徑），發現流動性幻覺問題，確立 Day 6 過濾策略 |

---

## 調整後計畫

### Phase 2：掃描品質 + 信號驗證（Day 06–08）

| Day | 主題 | 核心任務 |
|-----|------|---------|
| **06** | 流動性過濾 + 真實機會識別 | 加 `MIN_Q_STAR` / `MIN_POOL_TVL` 過濾；區分「幻覺」vs「真實但被搶走」；跑 30 分鐘看過濾後還剩幾條路徑 |
| **07** | 歷史回測框架 | 用過去 block 的 reserves 重播，找出「曾經存在過的機會」；統計機會持續時間、規模分佈 |
| **08** | p_win 校準 | 用 Flashbots bundle 歷史資料做 logistic 回歸，校準 `BribeModel` 的 k / midpoint（目前是猜測值） |

### Phase 3：執行準備（Day 09–14）

| Day | 主題 | 核心任務 |
|-----|------|---------|
| **09** | Flashbots bundle | 接 `eth_sendBundle`，把 scanner 找到的機會轉成 bundle；venue 從 "public" 改 "bundle" |
| **10** | 動態 gas | 接 `eth_gasPrice` / EIP-1559 basefee，讓 `ChainParams` 即時更新，不再寫死 $5.50 |
| **11** | 端對端串接 | scanner → EV 決策 → bundle 準備 → 模擬執行（不送鏈）全流程 |
| **12** | 小額模擬交易 | 用極小金額（$10 USDC）在測試網驗證整條流程 |
| **13** | 風控 checklist | 止損機制、最大單筆規模、異常偵測 |
| **14** | 緩衝 / 補漏 | 補前兩週技術債，準備進入執行期 |

### Phase 4：真實交易（Day 15–21）

| Day | 主題 | 核心任務 |
|-----|------|---------|
| **15** | 第一筆真實交易 | Roy 確認後執行，記錄完整 P&L |
| **16–19** | 迭代優化 | 根據實際結果調整 bribe_ratio、Q_bounds、過濾條件 |
| **20** | 成果盤點 | 統計 21 天的機會次數、成功率、實際 P&L vs EV 預測 |
| **21** | 總結 + 下一步 | 21 天共學結案，規劃後續方向（自動化、更多鏈、更多策略） |

---

## 關鍵待辦（跨 Day 的長期任務）

| 任務 | 預計 Day | 說明 |
|------|---------|------|
| `MIN_Q_STAR / MIN_POOL_TVL` 過濾 | Day 06 | 排除流動性幻覺，tri_scanner 結果才可信 |
| p_win sigmoid 校準 | Day 08 | 目前 k=5.65 / midpoint=0.95 是猜測值，⚠️ |
| Flashbots bundle | Day 09 | venue 從 "public" → "bundle"，f_cost 歸零 |
| 動態 gas | Day 10 | ChainParams 目前寫死 $5.50 |
| 真實交易 | Day 15 | 必須 Roy 逐筆確認，不自動執行 |

---

## 目前架構

```
onchain-arb/
├── models/
│   └── ev_model.py          # EV 模型（v2 AMM，verify_all 通過）
├── scripts/
│   ├── fetch_pools.py       # eth_call getReserves → DuckDB（同 block batch）
│   ├── scanner.py           # 兩池跨 DEX scanner（go/no-go）
│   ├── tycho_scanner.py     # 全池兩腿掃描（2097池/22幣對）
│   ├── tri_scanner.py       # 三角套利掃描（v2+v3，7990路徑）★ Day 5 更新
│   └── db.py / init_db.py   # DuckDB 工具
├── data/
│   └── arb.duckdb           # pool_snapshots 表
└── notes/
    ├── day02_*.md
    ├── day03_*.md
    └── day05_v3_tri_scanner.md
```

---

*最後更新：Day 05（2026-08-09）*
