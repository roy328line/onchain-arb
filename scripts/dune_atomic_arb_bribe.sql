-- Day 8：atomic arb bribe_ratio 校準資料
-- 目標：從鏈上 atomic arb 還原 gross_profit 和 bribe，計算 bribe_ratio 分布
--
-- Table：dex_ethereum.atomic_arbitrages（swap 層）
--         ethereum.transactions（gas 和 coinbase transfer）
-- 注意：dex_ethereum.atomic_arbitrages 沒有直接的 bribe 欄位，
--       需要透過 tx_hash join transactions 取 priority_fee_per_gas 和 gas_used，
--       再加上 coinbase.transfer（如果有）。
--
-- 由於 coinbase.transfer 需要解 trace，這個查詢先用 priority_fee 當 bribe 下限估計。
-- Day 8 的目標是確認資料結構是否可用，不是追求精確值。

WITH arb_txs AS (
    -- 每筆 arb tx 的進出 token 和金額
    SELECT
        block_time,
        tx_hash,
        tx_from,
        -- 粗估 gross_profit：取 amount_usd 最大的 leg（入場 leg）
        MAX(amount_usd) AS max_leg_usd,
        -- 有多少個 swap
        COUNT(*) AS n_swaps,
        -- 套利的 token 清單
        ARRAY_AGG(DISTINCT token_sold_symbol) AS tokens_sold
    FROM dex_ethereum.atomic_arbitrages
    WHERE block_time > NOW() - INTERVAL '7' DAY
      AND amount_usd > 0
    GROUP BY 1, 2, 3
    HAVING COUNT(*) >= 2   -- 至少 2 個 swap 才是 arb
),

tx_fees AS (
    -- 從 transactions 取 gas 費用
    SELECT
        hash AS tx_hash,
        (priority_fee_per_gas * gas_used) / 1e18 AS priority_fee_eth,
        (priority_fee_per_gas * gas_used) / 1e18 * 2500 AS priority_fee_usd,  -- 粗估 ETH 價格
        gas_used,
        gas_price
    FROM ethereum.transactions
    WHERE block_time > NOW() - INTERVAL '7' DAY
)

SELECT
    a.block_time,
    a.tx_hash,
    a.n_swaps,
    a.max_leg_usd AS gross_profit_usd_est,    -- 粗估毛利
    f.priority_fee_usd AS bribe_usd_est,       -- priority fee 作為 bribe 下限
    f.gas_used,
    -- bribe_ratio = bribe / gross_profit（只有 surplus > 0 才有意義）
    CASE
        WHEN a.max_leg_usd > f.priority_fee_usd AND a.max_leg_usd > 0
        THEN f.priority_fee_usd / (a.max_leg_usd - f.priority_fee_usd)
        ELSE NULL
    END AS bribe_ratio_est,
    -- 規模分桶
    CASE
        WHEN a.max_leg_usd < 10  THEN 'S ($0-10)'
        WHEN a.max_leg_usd < 50  THEN 'M ($10-50)'
        WHEN a.max_leg_usd < 200 THEN 'L ($50-200)'
        ELSE 'XL ($200+)'
    END AS size_bucket,
    a.tokens_sold
FROM arb_txs a
JOIN tx_fees f ON a.tx_hash = f.tx_hash
WHERE
    f.priority_fee_usd > 0
    AND a.max_leg_usd > 0
ORDER BY a.block_time DESC
LIMIT 500
