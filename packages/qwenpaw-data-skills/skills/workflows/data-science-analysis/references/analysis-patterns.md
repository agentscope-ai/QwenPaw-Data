# Analysis Patterns Reference

Patterns for the analysis phase: selecting extrema, joining datasets, computing rates, handling time-series, and applying robust statistics. Consult when aggregating, joining, calculating rates/percentages, or handling missing periods.

## Table of Contents

1. [Extremum & Tie-Breaking](#extremum--tie-breaking)
2. [Grouping for Unique Counts](#grouping-for-unique-counts)
3. [Joins & Merges](#joins--merges)
4. [Ratio & Rate Calculation](#ratio--rate-calculation)
5. [Time-Series Handling](#time-series-handling)
6. [Signal Processing](#signal-processing)
7. [Forecasting & Evaluation](#forecasting--evaluation)
8. [Statistical Methods](#statistical-methods)
9. [Output Formatting](#output-formatting)
10. [Common Pitfalls](#common-pitfalls)

---

## Extremum & Tie-Breaking

Sort by the primary metric, then a secondary tie-breaker. Isolate **all** ties on the primary condition before applying the tie-breaker:

```python
df_sorted = df.sort_values(
    by=["primary_metric", "tie_breaker_col"],
    ascending=[False, True],  # adjust per query
)
primary_val = df_sorted["primary_metric"].iloc[0]
ties = df_sorted[df_sorted["primary_metric"] == primary_val]
result = ties.iloc[0]
print(f"Primary value: {primary_val}, Tie count: {len(ties)}")
print(f"Selected row:\n{result}")
```

For range/duration edge cases, define inclusion rules explicitly:

```python
overlap = df[(df["start"] <= window_end) & (df["end"] >= window_start)]   # inclusive
strict  = df[(df["start"] > window_start) & (df["end"] < window_end)]     # exclusive
df["duration_inclusive"] = df["end_year"] - df["start_year"] + 1
df["duration_exclusive"] = df["end_year"] - df["start_year"]
```

---

## Grouping for Unique Counts

When events span multiple rows, group by a **stable identifier** to avoid double-counting:

```python
grouped = df.groupby("conflict_id").agg({
    "start_year": "min",
    "end_year": "max",
    "participants": "first",
}).reset_index()

filtered = grouped[(grouped["start_year"] >= start_bound) & (grouped["end_year"] <= end_bound)]
print(f"Raw rows: {len(df)}, Unique: {len(grouped)}, Filtered: {len(filtered)}")
```

---

## Joins & Merges

### Pre-Merge Checklist

1. **Align granularities**: aggregate to a common level (county→state, daily→monthly).
2. **Normalize keys**: strip whitespace, standardize case, zero-pad ID strings.
3. **Deduplicate**: ensure entity keys are unique per granularity level.
4. **Verify overlap**: check intersection size before committing.
5. **Apply exclusions first**; **prevent leakage** (target out of background set).

```python
left_keys = set(df1["id"].unique())
right_keys = set(df2["id"].unique())
overlap = left_keys & right_keys
print(f"Overlap: {len(overlap)} / {len(left_keys)} left, {len(right_keys)} right")

merged = pd.merge(df1, df2, on="id", how="inner", validate="1:1")
```

Preserve leading zeros with string zero-padding — never numeric-coerce IDs:

```python
df1["station_id"] = df1["station_id"].astype(str).str.zfill(5)
df2["station_id"] = df2["station_id"].astype(str).str.zfill(5)
```

**Always log unmatched keys** after a left join to detect silent drops:

```python
merged = df1.merge(df2, on="key", how="left")
unmatched = merged[merged["right_col"].isna()]
if len(unmatched):
    print(f"WARNING: {len(unmatched)} unmatched: {unmatched['key'].unique()[:10]}")
```

When intersecting metadata IDs with primary data (biomedical linking), filter both sides to the common set to prevent `KeyError`s and silent loss:

```python
common = set(metadata["sample_id"].dropna()) & set(primary["sample_id"].dropna())
metadata = metadata[metadata["sample_id"].isin(common)]
primary  = primary[primary["sample_id"].isin(common)]
```

---

## Ratio & Rate Calculation

### Strict Scope Alignment

Numerator and denominator **must** share the exact same categorical, temporal, and geographic scope.

### Use Authoritative Totals

Use the dataset's grand total (including "Unknown"/unclassified) as the denominator:

```python
total_reports = df["report_count"].sum()   # includes all categories
fraud_rate = fraud_reports / total_reports
# WRONG: summing only known sub-categories skews the rate if "Unknown" is large
```

### Pooled vs. Average Rates

Distinguish **pooled** (grand totals) from **average of periodic rates** — compute exactly what the prompt asks:

```python
# Pooled overall rate
pooled = df["events"].sum() / df["trials"].sum() if df["trials"].sum() else 0

# Average of per-period rates
df["period_rate"] = df.apply(
    lambda r: r["events"] / r["trials"] if r["trials"] else pd.NA, axis=1
)
average = df["period_rate"].mean()
```

Guard against division by zero, and validate:

```python
print(f"Numerator: {numerator}, Denominator: {denominator}")
if numerator > denominator:
    print("WARNING: numerator exceeds denominator—check scope alignment")
```

**Recompute all derived metrics** (rates, percentages, ratios) from raw numerators/denominators — never trust pre-calculated values in source files.

---

## Time-Series Handling

Sort chronologically before any rolling/lag/diff:

```python
df = df.sort_values(["station_id", "date"])
df["temp_diff"] = df.groupby("station_id")["temperature"].diff()
```

Filter to the exact prompt range — don't rely on global extrema falling within it:

```python
df["date"] = pd.to_datetime(df["date"], errors="coerce")
df = df[(df["date"] >= "2000-01-01") & (df["date"] <= "2020-12-31")]
```

Interpolate missing target periods from bounding values:

```python
def interpolate_missing_year(df, year_col, value_col, target_year):
    df = df.sort_values(year_col)
    prior = df[df[year_col] < target_year]
    later = df[df[year_col] > target_year]
    if len(prior) and len(later):
        p, s = prior.iloc[-1], later.iloc[0]
        frac = (target_year - p[year_col]) / (s[year_col] - p[year_col])
        return p[value_col] + (s[value_col] - p[value_col]) * frac
    return None
```

Exclude partial-year data from temporal averages:

```python
df["year"] = df["date"].dt.year
counts = df.groupby("year").size()
complete = counts[counts >= 365].index   # or 12 for monthly
df = df[df["year"].isin(complete)]
```

Impute by temporal group from valid records only:

```python
monthly_medians = df[df["value"].notna()].groupby(df["date"].dt.month)["value"].median()
```

### Merging Multi-Source Series at Different Cadences

Resample higher-frequency data to the target cadence, normalize datetime formats/timezones, then inner-join on the timestamp index. Apply the exact time window at the **row level** — file-level metadata or filenames often extend beyond the target.

```python
hourly_daily = hourly_df.set_index("timestamp").resample("D").mean()
merged = pd.merge(daily_df, hourly_daily, on="timestamp", how="inner")
merged = merged[(merged["timestamp"] >= start) & (merged["timestamp"] <= end)]
```

**Verify temporal coverage from the actual data, not filenames.** Prefer high-resolution files over aggregated ones when both exist.

```python
print(f"Coverage: {merged['timestamp'].min()} to {merged['timestamp'].max()}")
```

---

## Signal Processing

Detect peaks/troughs with a signal-processing library rather than manual thresholding:

```python
from scipy.signal import find_peaks

peaks, props = find_peaks(signal, prominence=0.5, distance=10)   # maxima
troughs, _   = find_peaks(-signal, prominence=0.5, distance=10)  # minima (invert)
```

### Ranking Top-N Extrema

Sort the **detected peaks** by magnitude — never sort the raw series (that bypasses the detection algorithm's prominence/distance constraints):

```python
order = np.argsort(signal[peaks])[::-1]
top_n = peaks[order][:n]
```

### Sliding-Window Event Clustering

Group detections separated by less than the window duration into a single event to prevent overcounting the same physical phenomenon:

```python
import numpy as np

peak_times = np.sort(time[peaks])
gaps = np.diff(peak_times)
n_events = 1 + int((gaps > tolerance).sum())   # clusters separated by > tolerance
```

---

## Forecasting & Evaluation

### Chronological Train/Test Splits

**Never use random splits** for time-series — always split chronologically, after merging and filtering to valid samples:

```python
split_idx = int(len(data) * 0.7)   # percentage-based, not a hardcoded date
train, test = data.iloc[:split_idx], data.iloc[split_idx:]
```

### Lagged Features

Shift the target to align current inputs with future evaluation periods, then drop the NaNs the shift creates:

```python
df["target"] = df["value"].shift(-forecast_horizon)
df = df.dropna()
```

Distinguish the **forecast-issuance date** (when the prediction is made) from the **target period** (what is predicted). Never apply generic anti-leakage shifts that alter task-specified windows.

### Forecast Evaluation

Align predicted and observed values by exact date, verify coverage, then compute metrics on aligned data only:

```python
merged = pd.merge(forecasts, observations, on="date", how="inner")
assert merged["date"].min() >= eval_start and merged["date"].max() <= eval_end
rmse = np.sqrt(((merged["predicted"] - merged["observed"]) ** 2).mean())
```

Subsequent forecast files may append actual observations — check for appended ground truth. If the task specifies exact overlap points or context windows, preserve them exactly.

---

## Statistical Methods

- **Spearman correlation**: skewed data, outliers, or monotonic non-linear relationships.
- **Log transforms**: right-skewed outcomes before parametric tests.
- **Non-parametric tests** (Mann-Whitney, Kruskal-Wallis): significantly differing distributions.
- **Cumulative sums with boundary checks**: Pareto queries ("top 20% account for 80%").
- **Group-level evaluation**: group by primary entities to evaluate thresholds per group, not globally.

### Causal Inference

- Map variables to causal roles (treatment, outcome, confounders).
- Control for confounders via stratification or regression.
- Diagnose selection bias in observational interventions (pre/post without randomization).

### Domain Term Translation

Map prompt terms to exact quantitative definitions via the data dictionary:
- "Generally unsafe" air quality → EPA AQI > 150.
- "Damaged" properties → the specific column, not "threatened".
- "Residential property value" → the dollar-amount column, not a count.

---

## Output Formatting

```python
import json
from pathlib import Path

artifact_dir = Path("artifacts/<session_id>")
# Only for an active TaskGraph node explicitly supplied by the runtime:
# artifact_dir /= Path("<graph_id>") / "<node_id>"
artifact_path = artifact_dir / "answer.json"
artifact_path.parent.mkdir(parents=True, exist_ok=True)
answer_value = round(result, 2)  # exact requested precision, applied only here
with artifact_path.open("w") as f:
    json.dump({"answer": answer_value}, f)
```

Replace `session_id` with the current runtime value. Add the graph/node suffix
only when both IDs are supplied for an active TaskGraph node.

- Numeric answers: raw numbers, no units, no thousands separators, no prose.
- **Negative constraints** ("no explanation"): output only the bare answer value.

---

## Common Pitfalls

1. Assuming adjacent rows = consecutive time steps → always sort and verify.
2. Relying on global max/min to fall within the required range → explicitly filter.
3. Dropping NaN rows → prefer NaN-ignoring aggregations to avoid silent loss.
4. Numeric coercion of IDs → use string matching with zero-padding.
5. Inline shell commands for complex logic → write standalone scripts.
6. Guessing column meanings → consult the data dictionary first.
7. Including partial-year data in temporal averages → check for incomplete records.
8. Computing aggregations before filtering → filter first, then aggregate.
9. Rounding intermediate values → round only at final output.
10. Pooling when averaging is needed (or vice versa) → compute exactly what the prompt asks.
11. Leaving sentinel fill values (e.g., `9.99E32`) in the data → replace with NaN before any statistic/model.
12. Random train/test splits on time-series → always split chronologically.
13. Sorting the raw series to pick extrema → sort detected peaks to respect prominence/distance constraints.
14. Trusting file-level metadata for temporal coverage → verify from the actual data rows.
