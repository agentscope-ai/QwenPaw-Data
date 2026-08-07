# Data Ingestion & Cleaning Reference

Patterns for discovering, loading, and cleaning tabular data before analysis. Consult when encountering unfamiliar formats, messy data, encoding issues, or join-key mismatches.

## Table of Contents

1. [File Discovery & Inspection](#file-discovery--inspection)
2. [Excel Workbook Exploration](#excel-workbook-exploration)
3. [Scientific & Binary Formats](#scientific--binary-formats)
4. [Encoding & Header Detection](#encoding--header-detection)
5. [Numeric Casting & Sanitization](#numeric-casting--sanitization)
6. [Missing, Sentinel & Censored Values](#missing-sentinel--censored-values)
7. [String Cleaning & Categorical Profiling](#string-cleaning--categorical-profiling)
8. [Data Scale Verification](#data-scale-verification)
9. [Defensive Spreadsheet Formatting](#defensive-spreadsheet-formatting)
10. [Loading Partitioned Data](#loading-partitioned-data)
11. [HTML Table Parsing](#html-table-parsing)
12. [Schema Mapping & Entity Search](#schema-mapping--entity-search)

---

## File Discovery & Inspection

Always discover files dynamically; never assume filenames:

```python
from pathlib import Path
files = list(Path("input").rglob("*"))
```

**Prioritize the dataset whose name and schema most directly match the task's core entities.** Alternative or subset files may introduce mismatches.

Inspect raw structure before loading. Look for:
- True delimiters (not always commas), encoding markers (BOM)
- Metadata blocks before the header (e.g., "Source: EPA", "Updated: 2023-01-01")
- Multi-level headers (row 1: "Temperature"; row 2: "Min", "Max", "Avg")
- Embedded summary rows ("Total", "Average") and footer notes
- Custom missing indicators (`-999`, `"N/A"`, `"--"`)

```python
with open("input/data.csv", "r", encoding="utf-8-sig") as f:
    for i, line in enumerate(f):
        if i >= 20:
            break
        print(f"{i}: {line.rstrip()}")
```

---

## Excel Workbook Exploration

```python
import pandas as pd

xls = pd.ExcelFile("input/data.xlsx")
print(f"Sheets: {xls.sheet_names}")
df = pd.read_excel(xls, sheet_name="Sheet1", nrows=5)
```

**Prioritize metadata sheets first** — README, Legend, Metadata, Description, Info, Dictionary. They often contain column definitions, categorical value mappings, methodology, and exclusion criteria.

For large or complex workbooks, fall back to `openpyxl` read-only wrapped in `try/except`:

```python
from openpyxl import load_workbook
wb = load_workbook("input/data.xlsx", read_only=True)
print(f"Sheets: {wb.sheetnames}")
for sheet_name in wb.sheetnames:
    ws = wb[sheet_name]
    print(f"\n--- {sheet_name} ---")
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i >= 5:
            break
        print(row)
wb.close()
```

---

## Scientific & Binary Formats

Astronomy/heliophysics tasks often ship array or binary formats. **Print keys and shapes first** to identify the correct arrays before use.

### NumPy `.npz` / `.npy`

```python
import numpy as np

data = np.load("input/grid.npz")
print(data.files)              # list available arrays
for k in data.files:
    print(k, data[k].shape, data[k].dtype)
arr = data["potential"]        # use the actual array, not a synthesized field
```

### FITS (`.fits`)

```python
from astropy.io import fits

with fits.open("input/image.fits") as hdul:
    hdul.info()                # list HDUs (headers + data units)
    header = hdul[0].header
    data = hdul[1].data        # table or image array
    print(data.columns if hasattr(data, "columns") else data.shape)
```

### HDF5 (`.h5`)

```python
import h5py

with h5py.File("input/data.h5", "r") as f:
    f.visit(print)             # print all dataset paths
    dset = f["group/dataset"]
    print(dset.shape, dset.dtype)
    values = dset[:]
```

### Format specs & whitespace-delimited files

Read any provided format spec (`.fmt`, `.text`, `README`) to map columns and units. For simple whitespace-delimited numeric files, prefer lightweight parsing over `pandas`:

```python
import numpy as np

# Fixed numeric columns, skipping comment lines
arr = np.loadtxt("input/omni.dat", comments="#")

# Or with named columns / mixed types
arr = np.genfromtxt("input/data.text", names=True, dtype=None, encoding="utf-8")
```

> **Tool selection:** match the tool to the task. Use `numpy`/`csv` for simple extraction and whitespace-delimited files; reserve `pandas` for tabular joins and richer processing. Avoid over-engineering. If a specialized package (e.g., `astropy`, `h5py`) fails to install, fall back to core `numpy`/`scipy`.

---

## Encoding & Header Detection

Try `utf-8-sig` first (handles BOM), then fall back:

```python
def read_csv_robust(filepath):
    for encoding in ["utf-8-sig", "utf-8", "latin-1", "cp1252"]:
        try:
            df = pd.read_csv(filepath, encoding=encoding)
            for col in df.select_dtypes(include=["object"]).columns:
                df[col] = df[col].str.replace("\xa0", " ", regex=False)  # non-breaking spaces
            return df
        except (UnicodeDecodeError, UnicodeError):
            continue
    raise ValueError(f"Could not read {filepath} with any encoding")
```

Detect the true header row when metadata precedes it:

```python
raw = pd.read_csv("input/data.csv", header=None, nrows=20, encoding="utf-8-sig")
print(raw)  # identify the row index with real column names
df = pd.read_csv("input/data.csv", header=<detected_row>, encoding="utf-8-sig")
print(df.columns.tolist())  # use these exact strings for all selection
```

---

## Numeric Casting & Sanitization

Strip formatting before casting:

```python
def sanitize_numeric(series):
    if series.dtype == "object":
        series = (series.str.replace(",", "", regex=False)
                        .str.replace("$", "", regex=False)
                        .str.replace('"', "", regex=False)
                        .str.strip())
    return pd.to_numeric(series, errors="coerce")
```

Cast through `float` before `int` to handle decimal strings like `"42.0"`:

```python
df["int_col"] = df["float_col"].apply(lambda x: int(float(x)) if pd.notna(x) else None)
```

Handle composite strings (e.g., `"X or Y"` → average):

```python
import numpy as np

def parse_composite(val):
    if pd.isna(val):
        return np.nan
    s = str(val).strip()
    if " or " in s:
        parts = [float(p.strip()) for p in s.split(" or ")]
        return sum(parts) / len(parts)
    return float(s)
```

---

## Missing, Sentinel & Censored Values

```python
# Placeholders → NaN
df["col"] = df["col"].replace(["-", "NA", "N/A", "", " ", "NULL", "null"], np.nan)

# Sentinels → NaN before any aggregation
df = df.replace([-999, -9999, 999, 9999], np.nan)

# Space-physics fill values (OMNI/GOES etc.) — replace before ANY statistic/correlation/model
space_fills = [9.99e32, 999999.9, 99999.9, 9999.0, 999.9]
df = df.replace(space_fills, np.nan)
# For numpy arrays: arr[np.isin(arr, space_fills)] = np.nan
```

**Censored values** (below/above detection limit) — decide the rule before converting:

```python
def parse_censored(val):
    if pd.isna(val):
        return pd.NA
    val = str(val).strip()
    if val.startswith("<"):      # below detection limit: half the limit (or 0, per methodology)
        try:
            return float(val[1:]) / 2
        except ValueError:
            return pd.NA
    if val.startswith(">"):      # above detection limit: use the limit value
        try:
            return float(val[1:])
        except ValueError:
            return pd.NA
    try:
        return float(val)
    except ValueError:
        return pd.NA
```

---

## String Cleaning & Categorical Profiling

```python
# Strip whitespace from all string columns
str_cols = df.select_dtypes(include="object").columns
df[str_cols] = df[str_cols].apply(lambda c: c.str.strip())

# Case-insensitive matching (include na=False on mixed-type columns)
matches = df["category"].astype(str).str.contains("tumor", case=False, na=False)
```

**Always profile unique values before filtering** to catch case/whitespace/typo variations:

```python
print(df["tissue"].value_counts())
print(df["tissue"].unique())
```

> **Caution:** Do not normalize typos or split concatenated strings unless the task asks for it. Preserve exact literal counts when the task requests raw unique entries.

---

## Data Scale Verification

Confirm whether a column is already log-transformed before exponentiating:

```python
print(f"Min: {df['value'].min()}, Max: {df['value'].max()}")
# Small ranges (e.g., 0-20) suggest log-transformed; large ranges (0-10000) suggest raw.

if df["value"].max() < 50:  # heuristic for log2
    df["value_raw"] = 2 ** df["value"]
    print("Applied inverse log2 transformation")
```

Always print sample values before and after any transformation to verify.

---

## Defensive Spreadsheet Formatting

```python
# Multi-row headers: use a specific row as the header
df = pd.read_excel("data.xlsx", header=1)

# Or set names manually after skipping metadata
df = pd.read_excel("data.xlsx", header=None, skiprows=2)
df.columns = ["sample_id", "gene", "value", "unit"]

# Merged cells: forward-fill
df = pd.read_excel("data.xlsx").ffill()
```

Validate numeric columns to skip embedded metadata/footer rows:

```python
df[numeric_col] = pd.to_numeric(df[numeric_col], errors="coerce")
before = len(df)
df = df.dropna(subset=[numeric_col])
print(f"Skipped {before - len(df)} non-data rows")
```

---

## Loading Partitioned Data

When data is split across files (by year, region, etc.), iterate, load, and concatenate — **retaining the partition key as a column** and logging found vs. missing:

```python
dfs = []
for year in range(2015, 2024):
    path = f"input/data_{year}.csv"
    try:
        d = pd.read_csv(path)
        d["year"] = year  # retain partition key
        dfs.append(d)
        print(f"Loaded {path}: {len(d)} rows")
    except FileNotFoundError:
        print(f"Missing: {path}")

if not dfs:
    raise ValueError("No data files found")
combined = pd.concat(dfs, ignore_index=True)
```

Verify column names match across partitions; deduplicate if needed.

---

## HTML Table Parsing

```python
from bs4 import BeautifulSoup

def parse_html_table(html_content):
    soup = BeautifulSoup(html_content, "html.parser")
    table = soup.find("table")
    headers = [th.get_text(strip=True) for th in table.find_all("th")]
    rows = []
    for tr in table.find_all("tr"):
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        if cells:
            rows.append(cells)
    return pd.DataFrame(rows, columns=headers)
```

Extract years/categories dynamically from headers — never hardcode:

```python
import re

def extract_years_from_headers(headers):
    years = []
    for header in headers:
        m = re.search(r"\b(19|20)\d{2}\b", str(header))
        if m:
            years.append(int(m.group()))
    return sorted(years)
```

---

## Schema Mapping & Entity Search

Map domain terms from the prompt to exact column names:

```python
print("All columns:", df.columns.tolist())
mutation_cols = [c for c in df.columns if "mutation" in c.lower() or "variant" in c.lower()]
```

If an entity's location is unknown, brute-force string-search across cells before assuming the schema:

```python
def search_for_entity(df, entity_name):
    entity = entity_name.lower().strip()
    for col in df.columns:
        if entity in str(col).lower():
            print(f"Found in column name: {col}")
        matches = df[col].astype(str).str.lower().str.contains(entity, na=False)
        if matches.any():
            print(f"Found in column '{col}':\n{df[matches][[col]].head()}")
```

---

## Cleaning Checklist

- [ ] All files discovered via glob; data dictionaries/README read first
- [ ] Raw structure inspected; true header detected
- [ ] Encoding fallbacks implemented
- [ ] Numeric columns sanitized and coerced with `errors="coerce"`
- [ ] Missing placeholders, sentinels, and censored values handled
- [ ] String columns normalized; categorical values profiled
- [ ] Data scales verified (log vs raw)
- [ ] Partition keys retained when concatenating
