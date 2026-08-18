"""
Day 8 sigmoid 校準腳本
用法：python3 scripts/calibrate_bribe.py <csv_path>

CSV 格式（Dune 匯出）：
  block_time, tx_hash, n_swaps, gross_profit_usd_est,
  bribe_usd_est, bribe_ratio_est, size_bucket, tokens_sold
"""

import sys
import math
import numpy as np
import pandas as pd
from scipy.optimize import curve_fit
import matplotlib
matplotlib.use("Agg")  # 無 GUI 環境
import matplotlib.pyplot as plt

# ── 讀入資料 ──────────────────────────────────────────────
if len(sys.argv) < 2:
    print("用法: python3 scripts/calibrate_bribe.py <dune_export.csv>")
    sys.exit(1)

df = pd.read_csv(sys.argv[1])
print(f"總筆數: {len(df)}")
print(f"欄位: {list(df.columns)}")
print()

# 過濾有效資料
df = df[df["bribe_ratio_est"].notna()]
df = df[df["bribe_ratio_est"] > 0]
df = df[df["bribe_ratio_est"] <= 1.5]  # > 1.5 可能是異常
print(f"過濾後筆數: {len(df)}")
print()

# ── 各桶分析 ──────────────────────────────────────────────
buckets = ["S ($0-10)", "M ($10-50)", "L ($50-200)", "XL ($200+)"]

def sigmoid(r, k, mid):
    return 1 / (1 + np.exp(-k * (r - mid)))

results = {}
fig, axes = plt.subplots(2, 2, figsize=(12, 8))
axes = axes.flatten()

for i, bucket in enumerate(buckets):
    sub = df[df["size_bucket"] == bucket]["bribe_ratio_est"].dropna()
    ax = axes[i]

    if len(sub) < 5:
        print(f"[{bucket}] 樣本不足（{len(sub)} 筆），跳過")
        ax.set_title(f"{bucket}\n樣本不足 ({len(sub)})")
        continue

    # 經驗 CDF
    ratios = np.sort(sub.values)
    cdf_vals = np.arange(1, len(ratios) + 1) / len(ratios)

    # sigmoid 擬合
    try:
        popt, pcov = curve_fit(
            sigmoid, ratios, cdf_vals,
            p0=[5.0, 0.5],
            bounds=([0.1, 0.0], [50.0, 1.5]),
            maxfev=5000,
        )
        k_fit, mid_fit = popt
        perr = np.sqrt(np.diag(pcov))
        fit_ok = True
    except Exception as e:
        k_fit, mid_fit = float("nan"), float("nan")
        fit_ok = False
        print(f"[{bucket}] sigmoid 擬合失敗: {e}")

    results[bucket] = {
        "n": len(sub),
        "k": round(k_fit, 3),
        "midpoint": round(mid_fit, 3),
        "median_ratio": round(float(np.median(ratios)), 4),
        "p10": round(float(np.percentile(ratios, 10)), 4),
        "p90": round(float(np.percentile(ratios, 90)), 4),
    }

    # 畫圖
    ax.plot(ratios, cdf_vals, ".", alpha=0.3, label="empirical CDF")
    if fit_ok:
        r_line = np.linspace(0, 1.2, 200)
        ax.plot(r_line, sigmoid(r_line, k_fit, mid_fit), "r-",
                label=f"sigmoid k={k_fit:.2f}, mid={mid_fit:.2f}")
    ax.axvline(0.5, color="gray", linestyle="--", alpha=0.5)
    ax.set_xlabel("bribe_ratio")
    ax.set_ylabel("P(competitor ≤ r)")
    ax.set_title(f"{bucket} (n={len(sub)})")
    ax.legend(fontsize=8)
    ax.set_xlim(0, 1.2)
    ax.set_ylim(0, 1.05)

plt.tight_layout()
plt.savefig("notes/day08_bribe_cdf.png", dpi=120)
print("圖表已存：notes/day08_bribe_cdf.png")
print()

# ── 印出結果 ──────────────────────────────────────────────
print("=" * 65)
print("  bribe_ratio 分布 × sigmoid 擬合結果")
print("=" * 65)
print(f"{'桶':<15} {'n':>5} {'k':>7} {'mid':>7} {'p10':>7} {'p50':>7} {'p90':>7}")
print("-" * 65)
for bucket, r in results.items():
    print(f"{bucket:<15} {r['n']:>5} {r['k']:>7} {r['midpoint']:>7} "
          f"{r['p10']:>7} {r['median_ratio']:>7} {r['p90']:>7}")
print("=" * 65)

# ── 更新 BribeModel 建議 ──────────────────────────────────
print()
print("建議更新 ev_model.py 的 BribeModel：")
for bucket, r in results.items():
    if not math.isnan(r.get("k", float("nan"))):
        print(f"  # {bucket}")
        print(f"  BribeModel(k={r['k']}, midpoint={r['midpoint']})")
print()
print("⚠️ 注意：bribe_usd_est 只用 priority_fee（未含 coinbase.transfer）")
print("   實際 bribe_ratio 可能更高，以上數字為下限估計")
