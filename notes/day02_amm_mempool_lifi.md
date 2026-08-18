# Day 02 — AMM / Mempool / LiFi 套利基礎

> 目標：理解三個核心機制，為後續 scanner 與成本模型打底。

---

## 一、AMM（Automated Market Maker）

### 核心公式

$$x \cdot y = k$$

| 名詞 | 說明 |
|------|------|
| `x` | Token A 儲備量 |
| `y` | Token B 儲備量 |
| `k` | 恆定乘積（invariant） |
| Spot Price | $\text{Price}_{A} = y / x$ |

### 交易後價格漂移（Price Impact）

買入 $\Delta x$ 個 Token A 後：
- 新儲備：$x' = x - \Delta x$
- 系統須解 $y' = k / x'$，付出 $\Delta y = y' - y$
- **套利機會來源**：兩個池子的 Spot Price 不同

### 常見 AMM 類型

| 協議 | 公式 | 特點 |
|------|------|------|
| Uniswap v2 | $xy = k$ | 全價格範圍流動性 |
| Uniswap v3 | Concentrated Liquidity | 指定價格區間，資本效率高 |
| Curve | Stableswap (混合) | 穩定幣低滑點 |
| Balancer | 加權 $x^w y^{1-w} = k$ | 多代幣池 |

### 套利本質

```
Pool A 報價低 → 在 A 買入 → 在 B 賣出（或反向）
利潤 = 賣出所得 - 買入成本 - Gas - 手續費
```

---

## 二、Mempool（記憶池）

### 什麼是 Mempool？

- 交易廣播後、上鏈前暫存的「等待區」
- 每個節點有自己的 local mempool
- 礦工/驗證者從中挑選交易打包進區塊

### 關鍵概念

| 概念 | 說明 |
|------|------|
| **Pending Tx** | 已廣播但未打包的交易 |
| **Gas Price / Priority Fee** | 越高越優先被打包 |
| **Nonce** | 確保同一帳戶交易的順序性 |
| **Replace-by-Fee (RBF)** | 用更高 gas 替換同一 nonce 的交易 |

### Mempool 與套利的關係

```
你監控 mempool → 看到大額 swap 即將執行
→ 預測執行後的價格漂移（Price Impact）
→ 搶先（frontrun）或跟後（backrun）獲利
```

### Flashbots / MEV（Maximal Extractable Value）

| 機制 | 說明 |
|------|------|
| **MEV** | 礦工/驗證者藉由排序交易所能獲取的最大額外價值 |
| **Flashbots** | 私密交易通道（MEV-Boost），避免 mempool 公開競爭 |
| **Bundle** | 將多筆交易打包成原子操作送給 builder |
| **Sandwich Attack** | frontrun + backrun 包夾目標交易 |

### 監控工具

- `eth_subscribe("newPendingTransactions")` — WebSocket 監聽
- Bloxroute / 0x API — 私有 mempool stream
- Flashbots `eth_sendBundle` — 送 bundle 給 builder

---

## 三、LiFi（Li.Finance）

### 是什麼？

LiFi 是跨鏈橋接 + DEX 聚合器的**路由協議**。
一筆交易可以同時完成：跨鏈 + Swap + 最優路徑選擇。

官方文件：https://docs.li.fi/

### 核心架構

```
User → LiFi SDK/API
         ↓
   Route Finder（找最優路徑）
         ↓
   DEX Aggregator（Uniswap / 1inch / Paraswap...）
   + Bridge（Stargate / Hop / Across / Connext...）
         ↓
   Target Chain / Token
```

### 重要 API 端點（REST）

| 端點 | 說明 |
|------|------|
| `GET /quote` | 取得單一兌換報價 |
| `GET /routes` | 取得所有可用路由（含橋）|
| `GET /chains` | 支援的鏈清單 |
| `GET /tokens` | 支援的代幣 |
| `GET /tools` | 橋與 DEX 清單 |
| `POST /advanced/routes` | 進階路由（自訂參數）|

Base URL: `https://li.quest/v1`

### 快速範例（取得 USDC→USDT 報價）

```bash
curl "https://li.quest/v1/quote?\
fromChain=1&\
toChain=137&\
fromToken=USDC&\
toToken=USDT&\
fromAddress=0xYOUR_ADDRESS&\
fromAmount=1000000"
```

回傳重要欄位：

| 欄位 | 說明 |
|------|------|
| `estimate.toAmount` | 預期收到金額 |
| `estimate.gasCosts` | Gas 費用（含 bridge gas）|
| `estimate.feeCosts` | 協議費用 |
| `estimate.executionDuration` | 預估完成時間（秒）|
| `transactionRequest` | 直接可簽署的 tx 物件 |

### LiFi 與套利的切入點

1. **跨鏈價差偵測**：A 鏈買入 → LiFi bridge → B 鏈賣出
2. **路由成本建模**：把 `gasCosts + feeCosts + slippage` 帶入成本模型
3. **自動化查詢**：用 `/routes` 掃描多條路徑，找到淨利潤最高的

---

## 四、今日行動建議

- [ ] 用 curl 打一次 LiFi `/quote` API，感受回傳結構
- [ ] 在本地跑 `eth_subscribe` 監聽 10 分鐘 pending tx（可用 Alchemy WebSocket）
- [ ] 畫一張「AMM 價格漂移 → 套利機會」的流程圖（手寫也行）
- [ ] 閱讀 LiFi 文件的 [Getting Started](https://docs.li.fi/li.fi-api/li.fi-api)

---

*建立於 Day 02 (2026-08-06)*
