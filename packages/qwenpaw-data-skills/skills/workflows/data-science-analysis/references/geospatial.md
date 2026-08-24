# Geospatial Reference

Patterns for coordinate validation, distance calculations, and spatial joins. Consult for latitude/longitude queries, distance thresholds, and polygon intersections.

## Coordinate Validation

Cross-reference ranges to detect swapped lat/lon:

```python
print(f"Lat range: {df['lat'].min()} to {df['lat'].max()}")
print(f"Lon range: {df['lon'].min()} to {df['lon'].max()}")

if df["lat"].abs().max() > 90:   # latitude out of valid range → likely swapped
    df["lat"], df["lon"] = df["lon"], df["lat"]
    print("Swapped lat/lon columns")
```

## Distance Calculations

Use **Euclidean** for degree-based thresholds, **Haversine** for physical distances (km/mi):

```python
import numpy as np

def euclidean_deg(lat1, lon1, lat2, lon2):
    return np.sqrt((lat2 - lat1) ** 2 + (lon2 - lon1) ** 2)

def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371  # Earth radius in km
    lat1, lon1, lat2, lon2 = map(np.radians, [lat1, lon1, lat2, lon2])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = np.sin(dlat / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2) ** 2
    return R * 2 * np.arcsin(np.sqrt(a))
```

## Counting Unique Entities

Filter by attribute constraints **before** computing distances, then count distinct entities to prevent double-counting:

```python
filtered = df[df["attribute"] == "target_value"].copy()
filtered["distance"] = filtered.apply(
    lambda r: haversine_km(r["lat"], r["lon"], ref_lat, ref_lon), axis=1
)
within = filtered[filtered["distance"] <= threshold_km]
print(f"Matched rows: {len(within)}, Unique entities: {within['entity_id'].nunique()}")
```

---

## GeoPackage (GPKG) → Shapely Fallback

When `geopandas`/`fiona` fail to import or crash on a `.gpkg` file, extract geometries via `sqlite3` + `shapely`.

### Reading the geometry table

```python
import sqlite3
import shapely.wkb

conn = sqlite3.connect("data.gpkg")
table = conn.execute("SELECT table_name FROM gpkg_contents LIMIT 1").fetchone()[0]
geom_col = conn.execute(
    f"SELECT column_name FROM gpkg_geometry_columns WHERE table_name = '{table}'"
).fetchone()[0]

rows = conn.execute(f"SELECT * FROM {table}").fetchall()
col_names = [d[0] for d in conn.execute(f"SELECT * FROM {table} LIMIT 0").description]
geom_idx = col_names.index(geom_col)
```

### Stripping the GPKG binary header

GeoPackage prepends a proprietary header to standard WKB. Strip it before parsing:

```python
def gpkg_to_shapely(blob):
    """Strip GPKG binary header and parse the WKB payload."""
    if blob is None:
        return None
    flags = blob[3]
    env_type = (flags >> 1) & 0x07
    env_sizes = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}
    header_len = 8 + env_sizes.get(env_type, 0)
    return shapely.wkb.loads(blob[header_len:])
```

### Spatial joins with a bounding-box pre-filter

Brute-force pairwise intersection is slow; pre-filter by bounding box:

```python
from shapely.ops import unary_union

target_geom = unary_union([gpkg_to_shapely(r[geom_idx]) for r in target_rows])
tb = target_geom.bounds  # (minx, miny, maxx, maxy)

results = []
for row in source_rows:
    geom = gpkg_to_shapely(row[geom_idx])
    if geom is None:
        continue
    sb = geom.bounds
    if sb[2] < tb[0] or sb[0] > tb[2] or sb[3] < tb[1] or sb[1] > tb[3]:
        continue  # bounding boxes don't overlap
    if geom.intersects(target_geom):
        results.append(row)
```

### Multi-part geometries

Use `unary_union` to merge multi-part geometries (states with islands, disconnected fire perimeters) before intersection tests — more robust and faster than testing each part individually.

---

## Orbital Mechanics & Coordinate Systems

For astronomy/heliophysics tasks, **use established domain libraries** rather than hand-rolling physics equations. Track units carefully (km vs. meters, radians vs. degrees).

### Coordinate conversions

```python
import pymap3d

lat, lon, alt = pymap3d.ecef2geodetic(x, y, z)   # ECEF (meters) → geodetic
lon = lon % 360                                    # normalize to 0-360 (or use -180..180)
```

Check the target grid's longitude convention (0-360 vs. -180..180) and convert accordingly.

### Spatial grid interpolation

Inspect grid structure (axes, shapes, coordinate system) and verify dimension ordering — e.g. `(lat, lon, alt)` vs `(lon, lat, alt)` — before interpolating. Use the actual provided field arrays, not synthesized uniform fields, and handle longitude wrapping at the 360/0 boundary.

```python
import numpy as np
from scipy.interpolate import RegularGridInterpolator

data = np.load("input/grid.npz")
print(data.files)                       # inspect available arrays
potential = data["potential"]           # actual array, not g*altitude
interp = RegularGridInterpolator((data["lat"], data["lon"], data["alt"]), potential)
```

### Orbital analysis

- Use TLE epochs directly when comparing positions at specific times; avoid unnecessary propagation — direct pairwise altitude/position checks often suffice.
- Use dedicated libraries: `sgp4`, `skyfield`, or `astropy` for orbital calculations.
- TLE data uses specific units (km, radians, etc.) — verify before applying formulas.

## Notes

- Avoid external lookups (e.g., state-abbreviation maps) when the required identifiers already exist in the dataset.
- Prefer shapely or this GPKG fallback over fragile manual ray-casting point-in-polygon implementations.
