---
name: data-science-analysis
description: Computes a numeric or categorical answer to a quantitative data-science question by cleaning and analyzing local data files (CSV, Excel, TSV, and scientific formats .npz/.fits/.h5) with pandas, numpy, and scipy. Use whenever a task ships its own dataset (in whatever local directory it provides) and asks you to derive a value via statistical, geospatial, temporal, ratio, percentage, growth-rate, correlation, signal-processing, orbital, or forecasting analysis. Spans domains including archeology (demographics, paleoclimate proxies, radiocarbon dating, historical conflicts); biomedical/bioinformatics (tumor histology, genes, proteins, peptides, genomic variants); environmental (water quality, bacterial/Enterococcus exceedance, rainfall correlations, environmental justice); legal/consumer (fraud, identity theft, consumer complaints by state/metro); wildfire (incidents, causes, suppression costs, acres burned, fatalities, geospatial intersections); and astronomy/heliophysics/space weather (satellite telemetry TLE/Swarm/SP3, indices OMNI/GOES, solar activity, atmospheric density, orbital mechanics, forecasting).
version: 2
---

# Data Science Analysis

A single, domain-agnostic workflow for answering quantitative questions from local datasets. Data files may live in any directory the task provides (e.g., `input/`, `data/`, the current working directory, or an explicit path in the prompt); first identify that data directory, then apply the workflow to it. The steps below apply to any domain (archeology, biomedical, environmental, legal, wildfire, astronomy/heliophysics/space weather, and beyond) and to any file type (tabular CSV/Excel or scientific array/binary formats like `.npz`, `.fits`, `.h5`). Domain-specific gotchas are flagged inline and detailed in `references/domain-notes.md`.

## Core Principles

| Principle | Why |
|-----------|-----|
| **Python for all arithmetic** | Prevents LLM math errors |
| **Read files directly in scripts** | Never hardcode numbers from manual inspection—they drift and break |
| **Consult the data dictionary first** | Map every prompt term to exact columns/categories; never guess |
| **Single consolidated script** | For multi-step analysis, avoid many small `.py` files and inline `python3 -c` |
| **Print intermediate results** | Catches silent filtering/join errors before they reach the answer |
| **Recompute derived metrics from raw** | Never trust pre-calculated rates/percentages in source files |
| **Round only at the final step** | Intermediate rounding compounds error |

## Workflow

### 1. Discover & Profile

- Identify the data directory the task provides (e.g., `input/`, `data/`, the current working directory, or an explicit path given in the prompt), then glob all files under it (e.g., `<data_dir>/**/*`) — data files, data dictionaries, metadata, format specs (`.fmt`, `.text`, `README`), helper scripts. Avoid overly restrictive filename filters that might exclude the correct data. Restrict data access to this directory.
- **Read data dictionaries / README / format specs first.** Map every prompt term (e.g., "damaged", "generally unsafe", "tumor") to its exact column or category — never guess. Cross-reference discovered schemas against format specs to confirm column mappings and units.
- Inspect raw structure before loading: read the first (and last) 10–20 rows raw (`header=None`) to locate the true header row, metadata/summary/footer rows, delimiters, timestamp formats, and encoding quirks (BOM → `utf-8-sig`).
- For Excel, list `sheet_names` via `pandas.ExcelFile`; prioritize README/Legend/Metadata sheets, then preview each data sheet.
- For scientific/binary files (`.npz`, `.fits`, `.h5`), print all keys and array shapes to identify the correct arrays before use.
- Print exact column names and sample values; use names—not indices—for all downstream selection.

> Run `scripts/explore_input.py [data_dir]` first to profile every file (pass the data directory; defaults to `input/`). See `references/data-ingestion-and-cleaning.md` for encoding, header-detection, Excel, and scientific/binary-format patterns.

### 2. Ingest Robustly

Write a **single Python script**. Apply these in the first execution to avoid retries:

- **Match the tool to the task complexity:** use lightweight `numpy`/`csv` for simple whitespace-delimited files and direct extraction; reserve `pandas` for tabular joins and richer processing. Avoid over-engineering (e.g., direct pairwise comparisons rather than full time-series propagation when they suffice).
- Handle encoding robustly: try `utf-8-sig` (BOM), fall back to `latin-1`/`cp1252`; strip non-breaking spaces.
- Strip whitespace from headers and string columns.
- Sanitize numeric strings (commas, currency symbols, quotes) before casting; coerce with `pd.to_numeric(..., errors='coerce')`.
- Cast through `float` before `int` (`int(float(x))`) to handle decimal strings like `"42.0"`.
- For large files, use memory-efficient loading (`usecols`, `dtype`, `chunksize`; Excel `read_only=True`).

### 3. Clean & Sanitize

- **Missing/malformed placeholders & sentinel fills:** map `'-'`, `'NA'`, `'N/A'`, blanks, and sentinels (`-999`, `-9999`, and space-physics fills like `9.99E32`, `999999.9`, `99999.9`, `9999`, `999.9`) to `NaN` before computing any statistic, correlation, or model—including them severely skews results.
- **Censored values:** handle detection-limit markers (`'<0.1'`, `'>100'`) explicitly before numeric conversion (see domain notes).
- **Duplicates & scaled integers:** drop duplicate timestamps, exclude non-data/metadata columns, and decode scaled-integer encodings before use.
- **String normalization:** `.str.strip().str.lower()` for categorical filters; include `na=False` in `.str.contains()`/`.str.match()` to avoid errors on mixed-type columns.
- **Profile unique values** in categorical columns before filtering to catch case/whitespace/typo variations.
- **Verify data scales:** confirm whether values are log-transformed before applying exponentiation; inverse-transform only when raw metrics are required.
- Prefer NaN-ignoring aggregations over dropping rows to avoid silent data loss.

> **Caution:** Do not normalize typos or split concatenated strings unless the task asks for it—preserve exact literal counts for raw-unique-entry queries. See `references/data-ingestion-and-cleaning.md`.

### 4. Integrate & Join

- **Identify linking keys** (sample IDs, gene symbols, entity names, station IDs) across tables.
- **Normalize join keys** on both sides: strip whitespace, standardize case, zero-pad ID strings (never numeric-coerce IDs with leading zeros).
- **Align granularities** before merging (e.g., county → state, daily → monthly).
- **Verify key overlap empirically** (`len(inner)` vs `len(left)`) and **log unmatched keys** to detect silent drops.
- **Apply exclusion criteria first** (e.g., remove samples "not in the study") before extracting features.
- **Prevent data leakage:** when comparing a target entity to a background set, filter the target out of the background.

> See `references/analysis-patterns.md` § Joins and `references/domain-notes.md` for entity normalization (MSA/County suffixes) and biomedical linking.

### 5. Filter

- **Apply all spatial, temporal, and categorical filters before computing any aggregations.**
- Translate qualitative constraints (`'summer'`, `'preceding days'`, `'generally unsafe'`) into precise numeric/datetime bounds via the data dictionary.
- **Group by stable identifiers** (conflict ID, entity ID) rather than ambiguous names to prevent double-counting multi-row entries.
- **Print intermediate row counts** after each filter to verify the subset matches the prompt's scope. Ensure denominators reflect only the filtered subset.

### 6. Analyze

- **Extremum / closest-value queries:** sort by primary metric, then secondary tie-breaker; isolate *all* ties matching the primary condition before applying the tie-breaker. Print tie counts.
- **Rates & ratios:** align numerator and denominator to the exact same categorical/temporal/geographic scope. Distinguish **pooled overall rate** (grand totals) from **average of periodic rates** (mean of per-period rates)—compute exactly what the prompt asks. Guard against division by zero.
- **Time-series:** sort chronologically before any lag/rolling/diff. Filter to the exact prompt range (don't rely on global extrema); when merging multi-source series, resample to a common cadence and inner-join on timestamp. Verify temporal coverage from the actual data, not filenames. Handle gaps and interpolate missing periods; exclude partial-year data from temporal averages.
- **Signal processing:** detect peaks/troughs with `scipy.signal.find_peaks` rather than manual thresholding; rank top-N extrema by magnitude (not raw data); cluster events within a tolerance window to avoid overcounting. See `references/analysis-patterns.md` § Signal Processing.
- **Spatial & orbital:** validate coordinate ranges (detect swapped lat/lon); filter by attributes before computing distances; count distinct entities to prevent double-counting. For orbital/coordinate work, use domain libraries (`astropy`, `sgp4`/`skyfield`, `pymap3d`), normalize longitude conventions, and track units. See `references/geospatial.md`.
- **Forecasting:** use strict chronological train/test splits (never random); align lagged features and distinguish forecast-issuance from target dates; evaluate on date-aligned observed vs. predicted values; respect task-specified windows exactly. See `references/analysis-patterns.md` § Forecasting & Evaluation.
- **Statistics:** choose robust methods—Spearman for skewed/outlier data, log-transforms for right-skewed outcomes, non-parametric tests for distribution shifts. For causal questions, map variables to roles (treatment/outcome/confounder) and diagnose selection bias.
- **Domain scales:** align units before analysis (e.g., paleo BP counts backwards—"later" = minimum BP). See `references/domain-notes.md`.

> See `references/analysis-patterns.md` for tie-breaking, rate, time-series, signal-processing, forecasting, and statistical patterns.

### 7. Validate

Print intermediate results at every stage:

- Frequency distributions of parsed values and pre/post-filter row counts.
- Matched subsets (city pairs, filtered rows) and tie-breaking subsets to confirm correct records.
- Boundary-straddling edge cases in range filters; pre/post-merge overlap sizes.
- Sanity checks (e.g., warn if numerator exceeds denominator).

### 8. Output

Save the final answer under the current artifact directory supplied by the
QwenPaw Data runtime:

```text
Direct chat:        artifacts/<session_id>/answer.json
TaskGraph node:     artifacts/<session_id>/<graph_id>/<node_id>/answer.json
```

```json
{"answer": <value>}
```

- Always use the current `session_id`. Append `graph_id/node_id` only when the
  runtime explicitly supplies an active TaskGraph node; never invent IDs.
- Do not write answer files directly under the shared `artifacts/` root.
- Ensure the current artifact directory exists before writing.
- Use the `write_file` tool (not shell echo/redirection) for proper JSON formatting.
- Match exact requested precision; omit units, thousands separators, markdown, and conversational filler.
- Obey negative constraints (e.g., "No explanation needed")—output only the direct answer value.
- In the text response, cite source files/sheets and summarize filtering/exclusion logic; list actual entities when counting or identifying specific items.

## Environment Setup

```bash
pip install pandas openpyxl numpy scipy statsmodels
```

Add `shapely` (and optionally `geopandas`) for geospatial tasks; `beautifulsoup4` for HTML tables; `h5py`/`astropy` for `.h5`/`.fits` files; `astropy`, `sgp4`/`skyfield`, `pymap3d` for orbital mechanics and coordinate conversions. If a specialized package fails to install, fall back to core `numpy`/`scipy`. Rely solely on local files—no web access.

## Additional Resources

| Resource | When to use |
|----------|-------------|
| `scripts/explore_input.py` | Run at the start of every task to profile all files in the task's data directory (pass it as an argument; defaults to `input/`) |
| `references/data-ingestion-and-cleaning.md` | Encoding, header detection, type coercion, sentinel fills, censored values, Excel/HTML/scientific-binary parsing, join-key mismatches |
| `references/analysis-patterns.md` | Tie-breaking, joins, rate/ratio calculation, time-series, signal processing, forecasting, statistics, causal inference |
| `references/geospatial.md` | Coordinate validation, distance calculations, GPKG → shapely fallback, orbital mechanics & coordinate systems |
| `references/domain-notes.md` | Domain-specific knowledge (paleo/temporal scales, biomedical, environmental, legal, wildfire, astronomy/heliophysics/space weather) |
