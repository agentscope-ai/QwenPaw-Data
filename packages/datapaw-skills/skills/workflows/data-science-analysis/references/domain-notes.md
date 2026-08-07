# Domain-Specific Notes

Specialized knowledge that the general workflow does not cover. Read the section matching the task's domain; the vocabulary in the prompt (e.g., radiocarbon, tumor, Enterococcus, MSA, acres burned) signals which one applies.

## Table of Contents

1. [Archeology & Paleo / Temporal Scales](#archeology--paleo--temporal-scales)
2. [Biomedical & Bioinformatics](#biomedical--bioinformatics)
3. [Environmental](#environmental)
4. [Legal & Consumer Data](#legal--consumer-data)
5. [Wildfire](#wildfire)
6. [Astronomy / Heliophysics / Space Weather](#astronomy--heliophysics--space-weather)

---

## Archeology & Paleo / Temporal Scales

**BP dates count backwards from the present** (present = 1950 AD): larger BP = older. "Later" / "more recent" = **minimum** BP value. Sort descending by age for forward-time order.

```python
# BP → kyr (thousands of years before present)
df["age_kyr"] = df["age_bp"] / 1000

# Cal. BC → BP
df["age_bp"] = 1950 + df["cal_bc"].abs()

# Forward-time ordering for change-over-time calculations
df_sorted = df.sort_values("age_bp", ascending=False)
df_sorted["change"] = df_sorted["value"].diff()
```

- Use **calibrated** date columns (e.g., `Cal. BC`) rather than raw uncalibrated radiocarbon dates when the dataset implies calibrated timescales.
- **Align units before analysis** (BP ↔ kyr) — a common source of silent errors.

Interpolation with unit verification — verify bracketing points and bounds:

```python
import numpy as np

valid = df.dropna(subset=["time", "value"]).sort_values("time")
times, values = valid["time"].values, valid["value"].values
print(f"Data range: {times.min()} to {times.max()}")

result = np.interp(query_time, times, values)
idx = np.searchsorted(times, query_time)
print(f"Bracketing: ({times[idx-1]}, {values[idx-1]}), ({times[idx]}, {values[idx]})")
```

---

## Biomedical & Bioinformatics

- **Read README/Legend/Metadata sheets first** in Excel workbooks — they define columns, categorical values, and exclusion criteria. When multiple sheets/files hold similar metrics, use these to identify the dataset the task intends.
- **Check for log-transformed values** (often log2) before applying exponentiation; inverse-transform only when raw metrics are required. See `data-ingestion-and-cleaning.md` § Data Scale Verification.
- **Linking keys**: sample IDs, gene symbols. Intersect valid metadata IDs with primary data before joining (see `analysis-patterns.md` § Joins).
- **Apply exclusion criteria first** (e.g., remove samples "not in the study") before extracting features.
- **Prevent data leakage**: when comparing a target entity to a background set, exclude the target from the background.

```python
# Exclude target from background to prevent leakage
target_gene = "TP53"
background_genes = all_genes[all_genes != target_gene]
assert target_gene not in background_genes

# Decompose complex queries: filter population → apply exclusions → extract features → cross-reference
is_tumor = df["tissue"].str.lower() == "tumor"
is_primary = df["stage"].str.lower() == "primary"
filtered = df[is_tumor & is_primary]   # boolean masks, not chained indexing
```

- If an entity's location is unknown, **brute-force string-search across cells** before assuming the schema (see `data-ingestion-and-cleaning.md` § Entity Search).

---

## Environmental

- **Censored values** are common: `'<0.1'` (below detection limit), `'>100'` (above). Apply detection-limit rules before numeric conversion (see `data-ingestion-and-cleaning.md` § Censored Values).
- **Domain-specific missing markers**: `'M'`, `'ND'`, `'-'`, `'NA'` → map to null.
- **Partitioned data** (by year/region): iterate, load, concatenate, and **retain the partition key as a column**; log found vs. expected files.
- **Translate qualitative constraints** into precise bounds: `'summer'` → months [6, 7, 8]; `'preceding 7 days'` → a datetime range.
- **Rates**: distinguish pooled overall rate from average of periodic rates; accumulate numerators/denominators separately per group and guard against division by zero.
- **Temporal imputation**: when imputing by group (e.g., monthly medians), compute the baseline from only valid, non-null records within that group.
- **Group-level thresholds**: group by primary entities (e.g., beaches) to evaluate exceedance thresholds per group rather than globally.

---

## Legal & Consumer Data

- **Entity normalization** for joins on county/MSA/state names — strip domain suffixes and standardize punctuation:

```python
import re

def normalize_entity_key(name):
    if pd.isna(name):
        return None
    name = str(name)
    name = re.sub(r"\b(MSA|County|Parish|Borough|Metropolitan Statistical Area)\b",
                  "", name, flags=re.IGNORECASE)
    name = name.replace("–", "-").replace("—", "-").replace("‐", "-")  # unicode dashes → ASCII
    return re.sub(r"\s+", " ", name).strip().lower()
```

- **Authoritative totals**: use the dataset's grand total (including "Unknown"/unclassified) as the denominator for proportions — do not sum only known sub-categories.
- **Cross-jurisdictional entities**: filter out or apportion entities spanning multiple jurisdictions (e.g., multi-state MSAs) to prevent double-counting.
- **Time-series gaps**: interpolate missing years linearly from bounding periods; always load the baseline period immediately preceding the target range (see `analysis-patterns.md` § Time-Series).
- **Dynamic year extraction**: pull years from column headers/metadata — never hardcode.
- **Recompute derived metrics** (rates, percentages, ratios) from raw numerators/denominators; never trust pre-calculated values in source files.
- HTML tables appear in these datasets — see `data-ingestion-and-cleaning.md` § HTML Table Parsing.

---

## Wildfire

- **Read data dictionaries first** and map every prompt term (e.g., "damaged", "generally unsafe") to its exact column/category via the dictionary — never guess.
- **Sentinel missing values** (e.g., `-999`) must be replaced with `NaN` before aggregation.
- **Align granularities** before merging (county → state); deduplicate entity keys; verify key overlap empirically; use inner joins on overlapping periods to prevent NaN corruption.
- **Robust statistics**: Spearman for skewed/outlier-prone data, log-transforms for right-skewed outcomes, non-parametric tests for distribution shifts.
- **Causal reasoning**: diagnose selection bias in observational interventions; map variables to causal roles and control for confounders.
- **Time-series**: sort chronologically before rolling/window calculations; filter to the exact prompt range; verify consecutive steps and handle gaps/rollovers; **exclude partial-year data** (e.g., incomplete final-year records) from temporal averages.
- **Geospatial**: for state/area intersections with `.gpkg` files, use the GPKG → shapely fallback in `geospatial.md` when `geopandas`/`fiona` are unavailable.

---

## Astronomy / Heliophysics / Space Weather

Tasks in this domain ship their own input files (satellite telemetry and space-environment indices) and center on orbital mechanics, time-series forecasting, and statistical modeling.

- **File formats & datasets**: expect whitespace-delimited `.text`/`.dat` with `.fmt` format specs, and binary `.npz`/`.fits`/`.h5`. Common sources: TLE, Swarm, SP3 (telemetry); OMNI, GOES (space-environment indices); solar activity and atmospheric-density products. Read format specs and print keys/shapes before use (see `data-ingestion-and-cleaning.md` § Scientific & Binary Formats).
- **Sentinel fill values**: replace domain placeholders (`9.99E32`, `999999.9`, `99999.9`, `9999`, `999.9`) with `NaN` before any statistic, correlation, or model — including them severely skews results. Also drop duplicate timestamps, exclude non-data columns, and decode scaled-integer encodings.
- **Prefer high-resolution data** over low-resolution aggregated files when both are available.
- **Verify temporal coverage from the actual data**, not filenames or file-level metadata — coverage often overlaps or extends beyond target boundaries.
- **Respect exact analysis windows**: if the task specifies a precise context window or overlap, do not alter it with generic filtering or generic anti-leakage shifts.
- **Match tool to complexity**: prefer lightweight `numpy`/`csv` over `pandas` for simple whitespace-delimited files; use direct pairwise comparisons instead of full time-series propagation when they suffice. Avoid over-engineering.
- **No web access**: rely solely on local files and prompt instructions; never fetch external documentation.
- **Analysis specifics**: signal processing (`scipy.signal.find_peaks`, event clustering), orbital/coordinate conversions (`astropy`, `sgp4`/`skyfield`, `pymap3d`; ECEF↔geodetic; longitude normalization; `RegularGridInterpolator`), and forecasting (chronological splits, lagged features, RMSE by date alignment). See `analysis-patterns.md` §§ Signal Processing / Forecasting & Evaluation and `geospatial.md` § Orbital Mechanics & Coordinate Systems.
