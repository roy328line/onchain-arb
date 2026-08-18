# Day 13：DuckDB 時序記錄 + 機會確認

## 目標
連續掃描 + DuckDB 記錄，建立 net_real 時序資料，找出「曾經最接近正 EV 的時刻」。

## 完成的修正

### Bug 1：scan_results 表不存在
- 原本 `_db_insert_results` 的 `except Exception as e: pass` 靜默吃掉了所有錯誤
- 修法：每筆 insert 獨立 try/except，失敗時印出警告，不靜默失敗
- 在 `arb.duckdb` 建立 `scan_results` 表（schema 含 block, path, net_star_usd, net_real_usd, source, go_real）

### Bug 2：OBSERVE 候選從未跑 Quoter
- 原本只有 `net_star_usd > gas_cost ($5.50)` 的候選才建 entry（有 legs）
- 修法：加 `NEAR_MISS_THRESHOLD = $1.0`，只要 net_star_usd > $1 就建 entry + legs
- 每個 block 的 OBSERVE top 10 也跑 Quoter，記錄 net_real（source="quoter_obs"）

### 效果
- OBSERVE 候選數：243 → 322-329（+33% 路徑被追蹤）
- DuckDB 寫入：2400 筆 Quoter 驗證記錄，含 go_real=False 的 near-miss

## 關鍵發現：機會存在過（且持續存在）

```
路徑：DAI → WETH → USDT（0x6b1754→0xc02aaa→0xdac17f）
Q*  = 71.59 DAI
net_real_usd = -$1.05（Quoter 驗證）
出現頻率：565 次，每個 block 都出現（約 100+ 連續 blocks）
```

這不是偶發幻覺，是真實持續存在的套利缺口。

### 各路徑最佳 net_real 排名

| 路徑                   | 筆數 | best net_real | avg net_real | source     |
|------------------------|------|--------------|--------------|------------|
| DAI→WETH→USDT          | 565  | **-$1.05**   | -$56.44      | quoter     |
| DAI→USDC→USDT          | 195  | -$1.35       | -$73.45      | quoter     |
| WETH→USDC→USDT         | 23   | -$4.27       | -$19627      | quoter     |
| DAI→USDT→WETH          | 428  | -$7.84       | -$141.25     | quoter     |
| DAI→USDC→WETH          | 768  | -$8.68       | -$69.77      | quoter     |

## 核心問題的診斷

```
gas_cost     = $5.50（Ethereum mainnet，三角 tx）
net_real     = -$1.05
缺口         = $6.55
break-even gas = < $1.05
```

**問題是 gas，不是套利空間。** 市場上有人在吃這個缺口，但那個人的 gas < $1.05。

### 三種可能性

1. **Arbitrum/L2**：gas 費用 ~$0.1-0.5，這條路徑直接正 EV
2. **私有 relay（Flashbots）**：失敗不上鏈，f_cost=0，break-even gas 更低
3. **已被高頻套利者佔據**：每個 block 都看到，代表有人在追，但鏈上看不到（bundle）

## Day 14 計畫

### A. Arbitrum Quoter 接入
- 換 RPC 到 Arbitrum（alchemy/infura arbitrum endpoint）
- 接 Uniswap v3 Quoter on Arbitrum
- 驗同路徑（DAI→WETH→USDT 等效池）
- 預期：gas ~$0.20 → net_real ≈ +$4.30（正 EV）

### B. Gas 敏感性分析
- 在不同 gas 假設（$0.1, $0.5, $1.0, $2.0, $5.5）下計算 net_real 分布
- 找出「幾個 gas 以下就有正 EV 的路徑數量」
- 用現有 DuckDB 資料直接計算，不需要新的掃描

## 今日金句

> **net_real=-$1.05 不是失敗，是「機會確認」。**
> 套利空間存在，但 Ethereum mainnet 的 gas 把它全吃掉了。
> 下一步不是找更多路徑，是找更便宜的執行環境。
