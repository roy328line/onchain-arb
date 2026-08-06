# Day 02 — Mempool 觀察筆記

**日期：** 2026-08-06
**工具：** `eth_subscribe("newPendingTransactions")` via `wss://ethereum.publicnode.com`
**時長：** 10 分鐘

---

## 觀察結果

（WebSocket 跑完後填入）

- 看到幾筆 pending tx：___
- 其中幾筆是 DEX swap：___
- swap 佔比：___%

---

## 我看不到的是什麼？

### 1. Flashbots / MEV-Boost Bundle
- 走私密通道直送 block builder，**完全不進公開 mempool**
- 2022 年 The Merge 後，約 **90%+ 的以太坊區塊**由 MEV-Boost relay 打包
- 這意味著大量套利 bundle 在你看到之前就已經上鏈了

### 2. Bloxroute 私有 TX Stream
- 需要訂閱付費方案才能看到「fast lane」的 pending tx
- 免費節點只能看到已廣播到 P2P 網路的 tx，有延遲

### 3. Builder 私有 Mempool
- 大型 builder（如 Titan、rsync）有自己的私有 tx pool
- 某些錢包會直接把 tx 送給特定 builder，跳過公開廣播

### 4. 節點間的傳播延遲
- 你連的節點看到的 mempool ≠ 全網 mempool
- 地理位置、節點連線數都影響你看到的「快照」

---

## 關鍵結論

```
公開 mempool 看到的 = 全部 pending tx 的一小部分
MEV 最肥的機會（backrun 大單、三明治）= 在私密通道
→ 如果要認真做 MEV，需要：
  - Flashbots RPC（eth_sendBundle）
  - 或接 Bloxroute / 0x API 的 private tx stream
```

**對我們的 21 天計畫的意涵：**
- Day 2 的觀察目的達到：知道「我看不到什麼」
- Mempool 監聽作為 scanner 的信號源，需要在 Day 9 再深入
- 目前聚焦在 **DEX 池之間的靜態價差**（不依賴 mempool），不需要私密通道

---

*建立於 Day 02 (2026-08-06)*
