# 21 天鏈上套利共學 — 原計畫 vs 實際對照

> 2026-08-05 ～ 2026-08-26 ｜最後更新：Day 21

---

## 原計畫 vs 實際

| Day | 原計畫 | 實際做了什麼 | 為什麼偏移 |
|-----|--------|------------|-----------|
| 01 | EV 成本模型 | `ev_model.py`：AMM 模擬、optimal_size、verify_all() | 照計畫 |
| 02 | AMM / Mempool / LiFi | price impact curve、mempool 觀察（11,020 tx/10min）、LiFi API | 照計畫 |
| 03 | 真實池數據接入 | `fetch_pools.py`、`scanner.py`、3 個 bug 修正 | 照計畫 |
| 04 | 全池掃描 + 三角套利 | `tycho_scanner.py`（2097池）、`tri_scanner.py`（v2費用牆=90bps確認）| 照計畫 |
| 05 | v3 池接入 | 945池/7990路徑，流動性幻覺問題首次出現 | 照計畫，但發現了意外問題 |
| 06 | 流動性過濾 + 真實機會識別 | 加 MIN_POOL_TVL 過濾，確認幻覺 vs 真實 | 照計畫 |
| 07 | 歷史回測框架 | 歷史 block reserves 重播，機會持續時間統計 | 照計畫 |
| 08 | p_win 校準 | Flashbots bundle 歷史資料 + bribe 模型校準 | 照計畫 |
| 09 | Flashbots bundle 接入 | **停止 bribe 校準，轉向確認真實機會是否存在** | ⚠️ 大轉向：校準假設前提可能不成立 |
| 10 | 動態 gas | 確認動態 gas 是否能救 net | 計畫縮水，原本預期 gas 是主因 |
| 11 | 端對端串接 | EV 模型繼續修正，Leg 抽象層重構 | 沒有「真實機會」，串接目標不明 |
| 12 | 小額模擬交易 | Boros settle fee 研究（ANNUAL 確認） | 計畫失效：無真實機會可送 |
| 13 | 風控 checklist | tri_scanner 20 分鐘重跑，統計正 EV 數量 | 計畫失效 |
| 14 | 緩衝 / 補漏 | Day 9 code review 6 項修正、GitHub 整理 | 技術債清償 |
| 15 | 第一筆真實交易 | CrossEx Terminal API 串接、dynamic gas_cost | ❌ 無真實交易：機會不存在 |
| 16 | 迭代優化 | gas 診斷：$5.50→$0.09 無效，v3 tick 流動性幻覺根因確認 | 優化方向已無意義 |
| 17 | 迭代優化 | Quoter Q=$1→$2000 掃描，所有路徑 ROI ≈ -0.5% ～ -0.9% | 確認問題在哪，非 gas |
| 18 | 迭代優化 | 費率考古，最佳路徑 DAI→USDT→WETH→DAI，Q=$10 net=-$0.007 | 找 break-even 的下限 |
| 19 | 成果盤點（提前）| backrun_detector.py，WSS mempool，大 swap 頻率實測 | 轉向：嘗試 backrun 策略 |
| 20 | 成果盤點 | MEV-Share SSE 接通；AMM 手推 +$4 → 同日 500 blocks 證偽 | 自我證偽日：最重要的一天 |
| 21 | 總結 + 下一步 | README、roadmap、覆盤文章 | 照計畫 |

---

## 計畫 vs 實際：關鍵偏移點

### Day 09：最大的轉折

原計畫 Day 09 是「接 Flashbots bundle」，假設機會存在，只差執行層。

實際上：停下來問「機會真的存在過嗎？」——這個問題本應在 Day 01 就問。

後來 12 天全部是在回答這個問題。

### Phase 4 整個失效

原計畫 Day 15-21 是「真實交易 → 迭代優化 → 統計 P&L」。

實際上：Day 15 確認動態 gas 無效，Day 16 確認根因是流動性幻覺，Day 17-18 找 break-even，Day 19-20 嘗試 backrun，Day 20 下午自我證偽。

沒有一筆真實交易。但這不是失敗——這是正確答案。

### 計畫裡沒有的東西（但做了）

- `quoter_q_scan.py`：AMM 模型 vs Quoter 真實回報的系統性對比
- `backrun_detector.py`：mempool WSS 監聽，大 swap 頻率實測
- `backrun_falsify.py`：N+1 block 偏離存活性統計（**最重要的單一腳本**）
- MEV-Share SSE 接通
- Day 20 的自我證偽流程

---

## 三個問題的演進

### Q1：有三角套利機會嗎？

- Day 04：v2 費用牆 90bps，理論上很難
- Day 05：v3 接入，路徑多了，但幻覺也來了
- Day 17：Quoter 系統性掃描，確認 **net_real 全部為負**
- **答：沒有，v3 tick 幻覺讓模型持續高估機會**

### Q2：dynamic gas 能救 net 嗎？

- Day 15：動態 gas $0.083（不是 $5.50）
- Day 16：net 仍然全負
- **答：不能。問題不是 gas，是流動性幻覺**

### Q3：backrun 大額 swap 呢？

- Day 19：接 mempool，確認大 swap 頻率（每 10 分鐘 5 筆 $50k+）
- Day 20 上午：AMM 手推 +$4，看起來有機會
- Day 20 下午：500 blocks 實測，N+1 偏離存活率 **0%**
- **答：沒有。MEV bot 在同一個 block 內就清場了**

---

## 如果重來一次

1. **Day 01 就問**：「MEV bot 如何在 pending tx 層清場？Quoter 查到的是什麼狀態？」
2. **更早接 Quoter**：AMM 模型是好的思考框架，但不能代替鏈上實測
3. **更早做 N+1 存活性測試**：這個測試花了 2 小時跑，但值得在 Day 03 就做

---

## 下一步（如果繼續）

這 21 天確認了「散戶在 Ethereum mainnet 做 DEX 套利沒有空間」。但有幾個方向沒有探索：

| 方向 | 假設 | 需要驗證 |
|------|------|---------|
| 其他 L2（Base、Arb、Scroll）| 區塊時間更短，MEV 競爭更少 | 接 L2 Quoter，做同樣的 N+1 測試 |
| 長尾幣對 | 大 MEV bot 不覆蓋，流動性低但競爭也低 | 定義「長尾」門檻，重跑 backrun_falsify |
| Flashbots Protect | 直接在 bundle 層競爭 | 需要真實資金 + 接 `eth_sendBundle` |
| CEX-DEX 套利 | 資訊優勢在鏈下，不依賴 Quoter | 完全不同的架構，接 CEX WebSocket |

---

*最後更新：Day 21（2026-08-26）*
