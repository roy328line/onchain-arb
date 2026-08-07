# Day 03 — LiFi 機制深度學習筆記

> 日期：2026-08-07
> 目標：理解 LiFi 的路由機制、費用結構，並把費用正確接進成本模型

---

## 一、LiFi 是什麼（定位）

```
User
  │
  ▼
LiFi SDK / API         ← 你呼叫的入口
  │
  ├─ Route Finder       ← 枚舉所有可能路徑，排序最優
  │
  ├─ DEX Aggregator     ← 同鏈 swap（1inch、KyberSwap、Paraswap...）
  │
  └─ Bridge             ← 跨鏈（Across、CCTP、Stargate、Eco...）
         │
         ▼
    Target Chain / Token
```

**一句話定義：** LiFi 是一個「meta-router」，它不是 DEX 也不是 Bridge，
它是在所有 DEX 和 Bridge 之上的路由層，幫你找到 fromToken@fromChain → toToken@toChain 的最優路徑。

**2026-08-07 覆蓋範圍（實測）：**
- 支援鏈：69 條
- 橋：35 個
- DEX：35 個

---

## 二、路由機制

### 2.1 Step 的概念

LiFi 把每條路由拆成多個 **Step**：

| Step type | 說明 | 例子 |
|-----------|------|------|
| `swap`    | 同鏈 DEX swap | USDC→WETH via Uniswap |
| `cross`   | 跨鏈 bridge  | USDC ETH→USDC ARB via Across |
| `lifi`    | LiFi 自己執行的複合步驟 | swap+bridge 合一 |

**路由 = 一個或多個 Step 的組合。**

例：ETH 上 USDC → Arbitrum 上 WETH：
```
Step 1 [swap]  USDC → WETH (Uniswap, ETH)
Step 2 [cross] WETH ETH → WETH ARB (Across)
```
或者直接：
```
Step 1 [lifi]  USDC ETH → USDC ARB (CCTP，LiFi 幫你合並)
Step 2 [swap]  USDC → WETH (Uniswap, ARB)
```

### 2.2 排序邏輯

`/advanced/routes` 回傳多條路由，有 `tags` 標記：

| Tag | 意義 |
|-----|------|
| `RECOMMENDED` | LiFi 推薦（綜合考量速度/費用/可靠性） |
| `CHEAPEST` | 最低費用 |
| `FASTEST` | 最短時間 |

**實測（$5000 USDC ETH→ARB，14條路由）：**
```
路由 1  [RECOMMENDED, CHEAPEST]  Eco bridge       toAmount=$4987.50  gas=$0.08
路由 2  []                       Polymer bridge   toAmount=$4987.50  gas=$0.11
路由 3  []                       CCTP+Mayan       toAmount=$4987.43  gas=$0.09
```

---

## 三、費用結構（最重要，最容易搞錯）

LiFi 的費用分兩類，**行為完全不同**：

### 3.1 gasCosts

```json
"gasCosts": [
  {
    "type": "SEND",
    "amountUSD": "0.08",
    "token": { "symbol": "ETH" }
  }
]
```

- 這是你的錢包**額外支付**給礦工的 gas
- **不會**從 `fromAmount` 或 `toAmount` 扣除
- 用 ETH（或該鏈原生幣）支付
- 你在計算成本時要**額外加上**這筆

### 3.2 feeCosts

```json
"feeCosts": [
  {
    "name": "LIFI Fixed Fee",
    "amountUSD": "12.49",
    "token": { "symbol": "USDC" },
    "included": true          ← 關鍵欄位
  }
]
```

`included` 欄位是整個 LiFi API 最容易踩的坑：

| `included` | 意義 | 你的處理方式 |
|------------|------|------------|
| `true`  | 費用**已從 toAmount 扣除** | 不要再減，`toAmount` 已是淨值 |
| `false` | 費用**尚未扣除**，需額外支付 | 要再減去這筆 |

**⚠️ 最常見 bug：**
```python
# 錯誤：把 included=true 的費用再扣一次
real_output = to_amount - lifi_fixed_fee   # 重複扣！

# 正確：
real_output = to_amount                    # to_amount 已是淨值
extra_cost  = gas_cost_usd + sum(f for f if not f["included"])
```

### 3.3 實測費用拆解（$5000 USDC）

```
同鏈 USDC→WETH (ETH, KyberSwap)：
  gasCosts       : $0.25  （額外付 ETH）
  LIFI Fixed Fee : $12.49  included=True（已從 toAmount 扣）
  ─────────────────────────────────────
  你實際多付     : $0.25
  toAmount 已扣  : $12.49
  總損耗         : $12.74

跨鏈 USDC ETH→ARB (Eco bridge)：
  gasCosts       : $0.08  （額外付 ETH）
  LIFI Fixed Fee : $12.49  included=True（已從 toAmount 扣）
  ─────────────────────────────────────
  你實際多付     : $0.08
  toAmount 已扣  : $12.49
  總損耗         : $12.57
```

**結論：LiFi 固定費 ≈ $12.5（對 $5000 交易），佔 0.25%。**
小額套利（< $5000）用 LiFi 跨鏈幾乎不可能 EV > 0。

---

## 四、API 端點總覽

Base URL: `https://li.quest/v1`

| 端點 | Method | 說明 | 限制 |
|------|--------|------|------|
| `/quote` | GET | 單一最優路由報價 | 75 req / 2h（無 key）|
| `/advanced/routes` | POST | 所有可用路由（含排名）| 同上 |
| `/chains` | GET | 支援鏈清單 | 無限制 |
| `/tokens` | GET | 支援 token 清單 | 無限制 |
| `/tools` | GET | 所有橋 + DEX 清單 | 無限制 |
| `/status` | GET | 交易狀態查詢 | 無限制 |

### `/quote` 重要參數

```
fromChain     鏈 ID 或縮寫（1=ETH, 42161=ARB, 137=POL）
toChain       目標鏈
fromToken     token 地址或 symbol（USDC, ETH, WETH）
toToken       目標 token
fromAmount    金額（最小單位，e.g. USDC = 1e6 per $1）
fromAddress   你的錢包地址（必填，影響路由）
slippage      可接受滑點（預設 0.005 = 0.5%）
```

---

## 五、接進 ev_model.py 的方式

### 5.1 費用對應關係

```python
# LiFi quote 轉成 ev_model 的成本項
def lifi_quote_to_costs(quote: dict) -> dict:
    est = quote["estimate"]

    # 1. gas：額外付，直接加進 chain.base_gas_usd
    gas_usd = sum(float(g["amountUSD"]) for g in est.get("gasCosts", []))

    # 2. feeCosts：只加 included=False 的（included=True 已在 toAmount 扣了）
    extra_fee = sum(
        float(f["amountUSD"])
        for f in est.get("feeCosts", [])
        if not f.get("included", True)
    )

    # 3. 滑點損耗：from_usd - to_usd - included_fees（已含在 toAmount 差值裡）
    # 不需要額外計算，simulate_arb 的 net_raw 已涵蓋 AMM 滑點

    # 4. bridge_fee = 固定費（included=True，已扣）+ gas
    bridge_fee_usd = gas_usd + extra_fee

    return {
        "gas_usd":        gas_usd,
        "extra_fee_usd":  extra_fee,
        "bridge_fee_usd": bridge_fee_usd,  # 傳入 ChainParams.bridge_fee_usd
        "to_amount_is_net": True,           # toAmount 已是淨值，不要再扣 included 費用
    }
```

### 5.2 跨鏈套利的 EV 結構

```
同鏈套利：
  net_raw = simulate_arb(pool_a, pool_b, Q)["net"]

跨鏈套利（加 LiFi）：
  net_raw = to_amount - Q          ← LiFi quote 直接給你
  bridge_cost = lifi_gas + extra_fees
  ChainParams.bridge_fee_usd = bridge_cost
  surplus = max(0, net_raw - gas_total - bridge_cost)
  EV = p_win * surplus * (1 - r) - f_cost - h_cost
```

### 5.3 何時用 LiFi，何時不用

| 場景 | 是否用 LiFi |
|------|------------|
| 同鏈 DEX 套利（Uni v2 ↔ Sushi）| ❌ 不用，直接 simulate_arb |
| 跨鏈穩定幣套利（USDC ETH→ARB）| ✅ 用 LiFi quote 算橋費 |
| 跨鏈 ETH 價差套利 | ✅ 用 LiFi，但固定費 $12.5 要超過才有意義 |
| 小額測試（< $500）| ❌ LiFi 固定費比利潤大 |

---

## 六、今日行動清單

- [x] 打一次 `/quote`（同鏈 + 跨鏈，確認費用結構）
- [x] 打一次 `/advanced/routes`（看 14 條路由的排序邏輯）
- [x] 打一次 `/tools`（確認橋 35 個、DEX 35 個）
- [x] 理解 `included` 欄位的含義（不重複計費）
- [x] 寫出費用接入 ev_model.py 的方法
- [ ] Day 5：把 `lifi_quote_to_costs()` 整合進 ChainParams

---

## 七、關鍵數字記憶

| 項目 | 數字 |
|------|------|
| LiFi Fixed Fee（無 API key）| ~$12.5（對 $5000）= 約 0.25% |
| 跨鏈 gas（ETH mainnet）| $0.08–$0.25 |
| 免費 API 限制 | 75 req / 2h |
| 執行時間（Eco bridge）| 7 秒 |
| 執行時間（一般橋）| 數分鐘 ~ 20 分鐘 |
| 支援鏈數 | 69 條（2026-08-07）|

---

*建立於 Day 03 (2026-08-07)*
