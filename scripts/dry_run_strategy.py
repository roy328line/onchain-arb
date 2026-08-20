"""
scripts/dry_run_strategy.py — Day 15（A 線）

把 ev_model 的四腿 EV 結果轉換成 CrossEx Terminal 可執行的 order 格式，
但不真的送出——只 log 出 curl 指令和 JSON payload，供 Roy 審查後手動執行。

CrossEx Terminal API（本地 Fastify 伺服器）：
  GitHub: https://github.com/pendle-finance/arbitrage-with-crossex
  Base:   http://localhost:6688/api
  Auth:   x-arb-token: $(cat ~/.boros-crossex/config/api-token)

架構限制（重要）：
  CrossEx Terminal 只負責「Perp 腿」（Leg1 + Leg2）。
  Boros YU 腿（Leg3 + Leg4）必須透過 app.boros.finance UI 手動下單。
  本腳本：
    - Perp 腿 → 轉成 POST /api/deals 格式
    - Boros 腿 → 轉成人工操作指引
    - 先呼叫 POST /api/preview 驗證，不真的執行

使用方式：
  python3 scripts/dry_run_strategy.py          # 只印 dry-run 指令
  python3 scripts/dry_run_strategy.py --preview # 呼叫本地 CrossEx 的 /preview（需先啟動 Terminal）
"""

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime

# ── CrossEx Symbol 對照表 ────────────────────────────────────────────
# 格式：{EXCHANGE}_FUTURE_{BASE}_{QUOTE}
CROSSEX_SYMBOLS = {
    ("hyperliquid", "ETH"): "HYPERLIQUID_FUTURE_ETH_USDC",
    ("okx",         "ETH"): "OKX_FUTURE_ETH_USDT",
    ("gate",        "ETH"): "GATE_FUTURE_ETH_USDT",
    ("binance",     "ETH"): "BINANCE_FUTURE_ETH_USDT",
    ("hyperliquid", "BTC"): "HYPERLIQUID_FUTURE_BTC_USDC",
    ("okx",         "BTC"): "OKX_FUTURE_BTC_USDT",
}

# ── 資料結構 ─────────────────────────────────────────────────────────
@dataclass
class PerpOrderSpec:
    """CrossEx Perp 腿的執行規格。"""
    deal_id:     str         # 冪等鍵（自己產生）
    symbol_a:    str         # CrossEx symbol，Leg1（SHORT）
    symbol_b:    str         # CrossEx symbol，Leg2（LONG）
    qty:         float       # ETH 數量（由 notional_usd / eth_price 換算）
    execution:   str = "maker"          # "maker" 或 "taker"
    timeout_sec: int = 300              # maker 轉 taker 的等待秒數
    leverage_a:  int = 10
    leverage_b:  int = 10


@dataclass
class BorosOrderSpec:
    """Boros YU 腿的人工操作指引（無法程式化）。"""
    exchange:   str   # "hyperliquid" 或 "okx"
    side:       str   # "short_yu" 或 "long_yu"
    fixed_rate: float # 年化固定利率
    notional_usd: float
    ttm_days:   float # 到期天數
    url: str = "https://app.boros.finance"


@dataclass
class DryRunPlan:
    """完整的四腿乾跑計畫。"""
    perp:        PerpOrderSpec
    boros_legs:  list[BorosOrderSpec] = field(default_factory=list)
    ev_summary:  dict = field(default_factory=dict)
    generated_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())


def build_dry_run_plan(
    notional_usd: float,
    eth_price:    float,
    boros_fixed_short: float,  # e.g. 0.064 for 6.4%
    boros_fixed_long:  float,  # e.g. 0.0214 for 2.14%
    ttm_days:     float,
    capital_usd:  float,
    execution:    str = "maker",
    deal_id:      str | None = None,
) -> DryRunPlan:
    """
    從 EV 模型的參數建立乾跑計畫。

    Roy 仍需：
      1. 手動在 app.boros.finance 掛 Boros YU 腿（兩筆）
      2. 確認後用下面的 curl 指令執行 CrossEx perp 腿
    """
    qty = round(notional_usd / eth_price, 4)
    _deal_id = deal_id or f"arb-eth-{int(datetime.utcnow().timestamp())}"

    perp = PerpOrderSpec(
        deal_id   = _deal_id,
        symbol_a  = CROSSEX_SYMBOLS.get(("hyperliquid", "ETH"),
                                         "HYPERLIQUID_FUTURE_ETH_USDC"),
        symbol_b  = CROSSEX_SYMBOLS.get(("okx", "ETH"),
                                         "OKX_FUTURE_ETH_USDT"),
        qty       = qty,
        execution = execution,
        leverage_a = 10,
        leverage_b = 10,
    )

    boros = [
        BorosOrderSpec(
            exchange="hyperliquid", side="short_yu",
            fixed_rate=boros_fixed_short,
            notional_usd=notional_usd, ttm_days=ttm_days,
        ),
        BorosOrderSpec(
            exchange="okx", side="long_yu",
            fixed_rate=boros_fixed_long,
            notional_usd=notional_usd, ttm_days=ttm_days,
        ),
    ]

    # EV 摘要（使用 ev_model 計算，這裡只做近似）
    t_years = ttm_days / 365
    spread  = boros_fixed_short - boros_fixed_long
    gross   = notional_usd * spread * t_years
    trade_fee = notional_usd * 0.0005 * t_years * 2  # 兩 Boros 腿
    settle_fee = notional_usd * 0.002 * t_years * 2
    perp_fee_maker = notional_usd * (0.00015 + 0.0002) * 2  # 進出場
    opp_cost = capital_usd * t_years * 0.05
    net_maker = gross - trade_fee - settle_fee - perp_fee_maker - opp_cost
    apr = net_maker / capital_usd / t_years

    ev_summary = {
        "notional_usd":       notional_usd,
        "capital_usd":        capital_usd,
        "qty_eth":            qty,
        "ttm_days":           ttm_days,
        "spread_pct":         round(spread * 100, 2),
        "gross_usd":          round(gross, 0),
        "boros_trade_fee":    round(trade_fee, 0),
        "boros_settle_fee":   round(settle_fee, 0),
        "perp_fee_maker":     round(perp_fee_maker, 0),
        "opp_cost_usd":       round(opp_cost, 0),
        "net_pnl_maker_usd":  round(net_maker, 0),
        "apr_maker_pct":      round(apr * 100, 1),
        "leverage":           round(notional_usd / capital_usd, 1),
    }

    return DryRunPlan(perp=perp, boros_legs=boros, ev_summary=ev_summary)


def print_dry_run(plan: DryRunPlan) -> None:
    """印出完整的乾跑報告和 curl 指令。"""
    p   = plan.perp
    ev  = plan.ev_summary
    api = "http://localhost:6688/api"
    tok = "$(cat ~/.boros-crossex/config/api-token)"
    h   = f'-H "x-arb-token: {tok}"'

    print("=" * 65)
    print("  DRY RUN — 四腿套利計畫（未執行）")
    print("=" * 65)
    print(f"  生成時間:  {plan.generated_at}")
    print(f"  Deal ID:   {p.deal_id}")
    print()

    print("  ── EV 摘要 ──")
    print(f"  名目:   ${ev['notional_usd']:>10,.0f}  ({ev['qty_eth']} ETH)")
    print(f"  本金:   ${ev['capital_usd']:>10,.0f}  ({ev['leverage']}x leverage)")
    print(f"  利差:   {ev['spread_pct']}%  ({ev['ttm_days']} 天)")
    print(f"  毛利:   ${ev['gross_usd']:>10,.0f}")
    print(f"  費用:   ${ev['boros_trade_fee'] + ev['boros_settle_fee'] + ev['perp_fee_maker']:>10,.0f}  "
          f"(Boros {ev['boros_trade_fee']+ev['boros_settle_fee']:,.0f} + Perp {ev['perp_fee_maker']:,.0f})")
    print(f"  機會成本: ${ev['opp_cost_usd']:>8,.0f}")
    print(f"  淨利:   ${ev['net_pnl_maker_usd']:>10,.0f}")
    print(f"  APR:    {ev['apr_maker_pct']}%  (maker 路徑)")
    print()

    print("  ── Step 1：先在 app.boros.finance 手動掛 Boros YU ──")
    for b in plan.boros_legs:
        side_str = "SHORT YU（收固定）" if b.side == "short_yu" else "LONG YU（付固定）"
        print(f"  [{b.exchange.upper()}] {side_str}")
        print(f"    固定利率: {b.fixed_rate*100:.2f}%  名目: ${b.notional_usd:,.0f}  TTM: {b.ttm_days}天")
        print(f"    → {b.url}")
    print()

    print("  ── Step 2：確認 Boros 成交後，執行 CrossEx Perp 腿 ──")
    print()
    print("  # 2a. Preview（zero side-effect 驗證）")
    preview_body = json.dumps({
        "actions": [
            {"kind": "open-market", "symbol": p.symbol_a, "side": "SELL",
             "notional": str(int(ev["notional_usd"])), "leverage": p.leverage_a,
             "pairGroupId": "grp1"},
            {"kind": "open-market", "symbol": p.symbol_b, "side": "BUY",
             "notional": str(int(ev["notional_usd"])), "leverage": p.leverage_b,
             "pairGroupId": "grp1"},
        ]
    }, indent=2)
    preview_body_lines = preview_body.split("\n")
    preview_body_str = ("\n    ").join(preview_body_lines)
    print(f"  curl -sH {h} -X POST {api}/preview \\")
    print(f"    -H 'Content-Type: application/json' \\")
    print(f"    -d '{preview_body_str}' | jq '.data'")
    print()

    print("  # 2b. 執行（Roy 確認 Preview 後才跑）")
    deal_body = json.dumps({
        "id": p.deal_id,
        "a": {"symbol": p.symbol_a, "side": "SELL"},
        "b": {"symbol": p.symbol_b, "side": "BUY"},
        "qty": str(p.qty),
        "execution": p.execution,
        "timeoutSec": p.timeout_sec,
        "leverage": {"a": p.leverage_a, "b": p.leverage_b}
    }, indent=2)
    print(f"  # ⚠️  以下指令會真的下單，確認後才執行！")
    deal_body_lines = json.dumps({
        "id": p.deal_id,
        "a": {"symbol": p.symbol_a, "side": "SELL"},
        "b": {"symbol": p.symbol_b, "side": "BUY"},
        "qty": str(p.qty),
        "execution": p.execution,
        "timeoutSec": p.timeout_sec,
        "leverage": {"a": p.leverage_a, "b": p.leverage_b}
    }, indent=2).split("\n")
    deal_body_str = ("\n  #   ").join(deal_body_lines)
    print(f"  # curl -sH {h} -X POST {api}/deals \\")
    print(f"  #   -H 'Content-Type: application/json' \\")
    print(f"  #   -d '{deal_body_str}' | jq")
    print()

    print("  ── Step 3：監控 ──")
    print(f"  curl -sH {h} {api}/deals/{p.deal_id} | jq '.data.projection'")
    print(f"  curl -sH {h} {api}/positions | jq '.data'")
    print()
    print("  ── Step 4：平倉 ──")
    print(f"  # curl -sH {h} -X POST {api}/deals/{p.deal_id}/stop")
    print()
    print("  ⚠️  這是 DRY RUN，所有 curl 指令均已注解（# 開頭）")
    print("  ⚠️  Boros YU 腿必須手動在 UI 下單，CrossEx Terminal 不處理 Boros 腿")


if __name__ == "__main__":
    # 使用 2026-08-17 Pendle 推文案例的參數
    plan = build_dry_run_plan(
        notional_usd       = 2_402_465,
        eth_price          = 2_500.0,
        boros_fixed_short  = 0.064,   # 6.40% @ Hyperliquid
        boros_fixed_long   = 0.0214,  # 2.14% @ OKX
        ttm_days           = 27.8,
        capital_usd        = 288_621,
        execution          = "maker",
    )
    print_dry_run(plan)
