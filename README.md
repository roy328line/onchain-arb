# onchain-arb

21 天鏈上套利共學專案（2026-08-05 ～ 08-26）。

**核心問題：** 散戶用鏈上公開資料，還能找到真實的套利機會嗎？

**21 天後的答案：** 在以太坊 mainnet，幾乎不行。原因不是模型錯了，而是 MEV bot 在 pending tx 層就把機會清場了。Quoter 查到的永遠是均衡後的狀態。

這個 repo 完整記錄了我如何一步步確認「沒有機會」這件事，以及過程中建立的工具和對 EV 成本模型的理解。

---

## 核心發現（按重要性排序）

### 1. MEV bot 在 pending tx 層清場，Quoter 永遠看到均衡後狀態

這是整個 21 天最重要的一句話。你無法用 `eth_call` 在 latest block 查到「還沒被套走的機會」，因為 MEV bot 已經在同一個 block 或更早消滅了它。

驗證方式（`scripts/backrun_falsify.py`）：500 個 block 中的 $50k+ WETH/USDC、WETH/USDT swap，N+1 block 偏離 >0.1% 的比例 = **0%**（3 樣本 / 0 存活）。

### 2. v3 流動性幻覺：Quoter 高估 ≠ 真實可成交量

Uniswap v3 的 tick 機制讓 Quoter 在跨越 tick 邊界時回傳看似合理的報價，但真實可成交流動性遠低於計算值。2638 筆 Quoter 記錄，net_real 全部為負（`data/arb.duckdb` > `quotes` 表）。

### 3. 正確的 EV 成本模型要算雙邊費用

Day 20 review 修正的教訓：`meta_key="both"` 才是正確的預設。maker 路徑只算一邊費用，taker APR 從 9.7% 掉到 5.5%（ANNUAL）甚至 -34.9%（FLAT）。當測量說沒有，不要換一個估計來說有。

### 4. 三角套利的費用牆：v2 = 90bps、最佳 v3 路徑 ≈ 7bps

v2 三腿各 30bps，break-even 幾乎不可能跨越。v3 最佳路徑（DAI→USDT→WETH→DAI, 0.01%+0.01%+0.05%）僅 7bps，Q=$10 時 net=-$0.007——最接近 0，但仍負。

---

## 可以直接拿去用的工具

### `scripts/tri_scanner.py` — 三角套利掃描器
接 Tycho 串流（v2+v3），枚舉所有三角路徑，用 AMM 最優倉位公式掃描。

```bash
export TYCHO_API_KEY=your_key
SCAN_SECONDS=600 MIN_POOL_TVL=10000 python3 scripts/tri_scanner.py
```

### `scripts/quoter_q_scan.py` — Quoter Q 掃描
給定路徑 + Q 範圍，對比 AMM 模型預測 vs Quoter 真實回報，找 break-even。

```bash
export ETH_RPC=your_rpc
python3 scripts/quoter_q_scan.py
```

### `scripts/backrun_falsify.py` — Backrun 機會存活性驗證
爬取歷史大額 swap，比對 N 與 N+1 block 的 sqrtPriceX96，統計機會存活率。

```bash
export ETH_RPC=your_rpc
python3 scripts/backrun_falsify.py --blocks 500 --min-usd 50000
# 結果寫入 data/arb.duckdb > backrun_decay 表
```

### `models/ev_model.py` — EV 成本模型
從零建立的套利 EV 計算框架：gas 成本、AMM fee、Boros settle fee、bribe 模型、最優倉位計算。21 天持續修正，`verify_all()` 通過。

---

## 每日筆記（notes/）

每一天都有獨立的 `day{N}.md`，記錄：
- 當天做了什麼、為什麼這樣做
- 遇到什麼 bug 和怎麼解決
- 對之前結論的修正（包含為什麼修正）

特別值得看的：
- `notes/day09.md`：停止 bribe 校準，轉向確認真實機會是否存在過
- `notes/day17.md`：v3 tick 流動性幻覺的根因確認
- `notes/day20.md`：AMM 手推算出 +$4 → 同日證偽 → 為什麼這件事重要

---

## 環境設定

```bash
# 需要的環境變數
TYCHO_API_KEY=...      # Tycho 串流 API
ETH_RPC=...            # Ethereum mainnet RPC（支援 eth_getLogs）
QUICKNODE_RPC=...      # QuickNode（MEV-Share SSE 接通）

# 安裝依賴
pip install duckdb web3 eth-abi requests sseclient-py

# DuckDB schema 初始化
python3 scripts/init_db.py
```

`.env` 不進 git，`data/arb.duckdb` 不進 git（`.gitignore` 已設定）。

---

## 硬規則（不會自動執行的事）

- 任何真實資金的鏈上交易 —— 必須逐筆確認
- 任何 token approve —— 必須明確額度，永不 unlimited
- 私鑰 / 助記詞 → 不進任何檔案、log、對話、截圖

---

## 目錄結構

```
onchain-arb/
├── models/
│   └── ev_model.py          # EV 模型（AMM + fee + bribe，verify_all 通過）
├── scripts/
│   ├── tri_scanner.py       # 三角套利掃描器（v2+v3，Tycho 串流）
│   ├── quoter_q_scan.py     # Quoter Q 掃描，break-even 分析
│   ├── backrun_detector.py  # mempool 監聽，大額 swap 頻率實測
│   ├── backrun_falsify.py   # N+1 block 偏離存活性驗證（可重跑）
│   ├── dry_run_strategy.py  # CrossEx Terminal API 串接
│   ├── gas_monitor.py       # 動態 gas_cost（EIP-1559）
│   └── fetch_pools.py       # eth_call getReserves → DuckDB
├── data/
│   └── arb.duckdb           # quotes / backrun_decay / pool_snapshots 等
└── notes/
    ├── day01.md ～ day21.md  # 每日筆記
    └── roadmap.md           # 原計畫 vs 實際對照
```

---

*最後更新：Day 21（2026-08-26）*
