# Day 03 進度報告（2026-08-07）

> 給 Claude 二次確認用。包含：本日完成項目、新增檔案、Bug 修正、待辦事項。

---

## 一、本日目標

**選項 C：真實池數據接入 × Scanner 骨架合併**

打通完整鏈路：
```
公開 RPC (eth_call getReserves)
        ↓
4 個真實池 reserves → arb.duckdb (pool_snapshots 表)
        ↓
ev_model.best_ev() 計算最優規模 + EV
        ↓
go / no-go 決策輸出
```

---

## 二、新增檔案

### 1. `scripts/fetch_pools.py`

**功能：** 從 Ethereum 公開 RPC 節點抓取 AMM 池的真實 reserves，存入 DuckDB。

**關鍵設計：**
- 只用標準庫 + duckdb，不需要 web3.py
- 呼叫 `getReserves()` ABI selector `0x0902f1ac`，透過 `eth_call` 取得
- 多節點 fallback：`publicnode → 1rpc → mevblocker → blastapi`
- DuckDB 新建 `pool_snapshots` 表（`reserve0_raw` / `reserve1_raw` 用 `HUGEINT`，因為 uint112 超過 INT64 範圍）

**監控的 4 個池：**

| name | DEX | pair | pool address |
|------|-----|------|-------------|
| `uni_v2_usdc_weth` | Uniswap v2 | USDC/WETH | `0xb4e16d...` |
| `sushi_usdc_weth` | Sushiswap | USDC/WETH | `0x397ff1...` |
| `uni_v2_usdt_weth` | Uniswap v2 | WETH/USDT | `0x0d4a11...` |
| `uni_v2_dai_weth` | Uniswap v2 | DAI/WETH | `0xa478c2...` |

**執行方式：**
```bash
cd /home/ubuntu/onchain-arb
python3 scripts/fetch_pools.py
```

**實際輸出（2026-08-07）：**
```
📦 Block: 25700798 | 2026-08-07 04:46:06 UTC
  uni_v2_usdc_weth  ✅  USDC=8,869,616.69  WETH=4,641.15  spot=0.000523
  sushi_usdc_weth   ✅  USDC=123,095.20    WETH=64.41     spot=0.000523
  uni_v2_usdt_weth  ✅  WETH=3,892.32      USDT=7,444,755 spot=1912.68
  uni_v2_dai_weth   ✅  DAI=4,022,795.04   WETH=2,105.06  spot=0.000525
✅ 寫入 4 筆 → arb.duckdb / pool_snapshots
```

---

### 2. `scripts/scanner.py`

**功能：** 讀取真實 reserves，呼叫 `ev_model.best_ev()`，輸出套利機會的 go/no-go 決策。

**關鍵設計：**
- `ARB_PAIRS` 設定套利對（pool_a ↔ pool_b）
- `scan_pair()` 負責建 PoolState、計算 EV、輸出結果
- 支援 `--loop --interval 30` 持續掃描

**執行方式：**
```bash
# 單次
python3 scripts/scanner.py

# 持續掃描（每 30 秒）
python3 scripts/scanner.py --loop --interval 30
```

**實際輸出（修正後）：**
```
================================================================
  onchain-arb scanner | block 25700789 | 2026-08-07 04:46:33 UTC
================================================================

🔴 NO-GO  USDC/WETH: Uniswap v2 ↔ Sushiswap
  方向      : no_opportunity
  最優規模  : $3,405 USDC
  EV*       : $-1.2895
  淨毛利    : $-110.51
  Surplus   : $0.0000  (毛利 - gas)
  Gas 成本  : $5.50
  Bribe     : $0.0000  (ratio=100.00%)
  p_win     : 57.02%  ⚠️ sigmoid 未校準
  現貨價差  : 6.01 bps

────────────────────────────────────────────────────────────────
  掃描完成：1 對  |  GO: 0  NO-GO: 1
```

---

## 三、Bug 修正：pool_b x/y 方向設反

### 問題現象

`net_raw` 顯示 **-$3,405**，明顯異常（應該是幾十元的虧損，不是幾千元）。

### 根本原因

`simulate_arb(pool_a, pool_b, Q)` 的資金流向約定是：

```
Q token0 → pool_a（x=token0, y=token1）→ W token1
W token1 → pool_b（x=token1, y=token0）→ Q' token0
```

**pool_b 的 x 必須是 token1（我們投入的那側），y 是 token0（我們拿回的那側）。**

舊的 `scanner.py` 把 pool_b 也設成 `PoolState(x=USDC, y=WETH)`，等於把 1.77 WETH 當成 USDC 丟進去算，輸出 0.00093 WETH 當成 USDC，產生 -$3,405 的荒謬數字。

### 修正前後對比

```python
# ❌ 修正前（x/y 方向與 pool_a 相同）
pool_b = PoolState(
    x=snap_b[x_key],   # USDC ← 錯！pool_b 輸入的應該是 WETH
    y=snap_b[y_key],   # WETH ← 錯！pool_b 輸出的應該是 USDC
    fee=snap_b["fee_bps"] / 10_000,
)

# ✅ 修正後（x/y 方向翻轉）
pool_b = PoolState(
    x=snap_b[y_key],   # WETH ← 正確：我們投入 WETH
    y=snap_b[x_key],   # USDC ← 正確：我們拿回 USDC
    fee=snap_b["fee_bps"] / 10_000,
)
```

### 修正結果

| 指標 | 修正前 | 修正後 |
|------|--------|--------|
| `net_raw` | **-$3,405** ❌ | **-$110.5** ✅ |
| 含義 | 單位錯誤（WETH 被當 USDC 計算）| 6 bps 價差不夠覆蓋 gas，正確 |

### 手算驗證

```
Q = 3,405 USDC 投入 Uni v2
Step 1: 3,405 USDC → 1.7757 WETH（pool_a getOut）
Step 2: 1.7757 WETH → 3,292.86 USDC（pool_b getOut，正確方向）
net = 3,292.86 - 3,405 = -112.14 USDC  ✅（與 scanner 輸出 -110.51 吻合，差異來自最優化）
```

---

## 四、DuckDB 新增表：`pool_snapshots`

```sql
CREATE TABLE pool_snapshots (
    id           INTEGER,
    ts           TIMESTAMP,
    block        BIGINT,
    chain        VARCHAR,
    dex          VARCHAR,
    name         VARCHAR,
    pool_addr    VARCHAR,
    token0_sym   VARCHAR,
    token1_sym   VARCHAR,
    reserve0     DOUBLE,       -- human-readable
    reserve1     DOUBLE,
    reserve0_raw HUGEINT,      -- raw uint112（超過 INT64，用 HUGEINT）
    reserve1_raw HUGEINT,
    fee_bps      INTEGER,
    spot_price   DOUBLE        -- token1 per token0
)
```

> ⚠️ 踩坑記錄：DuckDB 的 `BIGINT` 是 INT64，Uniswap v2 的 `reserve` 是 uint112，最大值 ~5.19×10³³，遠超 INT64 上限。必須用 `HUGEINT`（INT128）才能存。

---

## 五、icl_*.py 說明

這四個腳本與套利無關，是之前探索 ICL 共學平台 API 用的工具：

| 檔案 | 用途 |
|------|------|
| `scripts/icl_test.py` | 連通測試，確認帳號 / 課程資訊 |
| `scripts/icl_api.py` | 查詢 `/me/check-ins` 和課程 events |
| `scripts/icl_explore.py` | 探索不同打卡 endpoint（POST /checkin 等）|
| `scripts/icl_peers.py` | 探索能否讀取其他學員的打卡記錄 |

打卡流程：Roy 丟原始筆記 → Claude 整理 → Roy 確認 → Claude 呼叫 `POST /me/check-ins` 送出。

---

## 六、現有架構總覽

```
onchain-arb/
├── models/
│   └── ev_model.py          # EV 成本模型（Day 1-2 建立，verify_all() 七項通過）
├── scripts/
│   ├── db.py                # DuckDB 連線工具
│   ├── init_db.py           # 建立 4 張核心表
│   ├── fetch_pools.py       # ★ Day 3 新增：真實 reserves 抓取
│   ├── scanner.py           # ★ Day 3 新增：套利機會掃描器（含 bug 修正）
│   └── icl_*.py             # ICL 打卡平台工具（非套利核心）
├── data/
│   └── arb.duckdb           # 包含 pool_snapshots 表
└── notes/
    ├── day02_amm_mempool_lifi.md
    ├── day02_mempool_obs.md
    └── day03_progress.md    # ← 本文件
```

---

## 七、已知限制 / 待辦

| 項目 | 狀態 | 說明 |
|------|------|------|
| 多幣對掃描 | ❌ 待做 | 現在只掃 1 對（Uni↔Sushi USDC/WETH） |
| 動態 gas | ❌ 待做 | 現在寫死 $5.50，應接 `eth_gasPrice` |
| scanner 結果存 DB | ❌ 待做 | 現在只輸出到 terminal，不累積歷史 |
| p_win sigmoid 校準 | ❌ 待 Day 8 | 目前 k=5.65 / midpoint=0.95 是猜測值 |
| Flashbots bundle | ❌ 待 Day 9 | 現在 venue="public"，bundle 成本為 0 |

---

*建立於 Day 03 (2026-08-07)*
