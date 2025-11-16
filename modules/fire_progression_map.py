#!/usr/bin/env python3
"""
fire_progression_map.py

Author: Mayar Ariss, MIT Senseable City Lab
Created: 2025-11-15

Build fire progression maps for a single incident using data
downloaded by scraper.py.

- Reads the CSV index produced by scraper.py
- Filters to a given year and incident name / substring
- Loads perimeter geometries (perimeter shapefiles and/or gdbs)
- Orders them in time
- Draws all perimeters at once, with color encoding time since first perimeter

CLI:

  python fire_progression_map.py \
      --data-root C:\\gb_ir_2015_2025 \
      --index ir_events_index.csv \
      --year 2021 \
      --incident "Scarface" \
      --out-html scarface_2021_progression.html
"""

#  Ensure required packages  installed
import importlib
import subprocess
import sys


def ensure_package(pkg_name: str):
    try:
        importlib.import_module(pkg_name)
    except ImportError:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", pkg_name],
        )


for _pkg in ("pandas", "geopandas", "folium"):
    ensure_package(_pkg)
# -------------------------------------------------------------------------

import argparse
import datetime as dt
import re
from pathlib import Path
from typing import Optional, List, Dict, Any

import pandas as pd
import geopandas as gpd
from shapely.geometry import mapping
import folium
from folium.features import GeoJsonTooltip
import branca.colormap as cm  

from branca.element import MacroElement
from jinja2 import Template

try:
    import fiona
except ImportError:
    fiona = None  # geopandas usually brings it in


# ---------------------------------------------------------------------------
# Helpers for timestamp logic
def parse_iso_or_none(s: str) -> Optional[dt.datetime]:
    """Parse ISO-like timestamp, or return None."""
    if not s or pd.isna(s):
        return None
    try:
        return dt.datetime.fromisoformat(str(s))
    except Exception:
        return None


def parse_ir_folder_date(folder_name) -> Optional[dt.datetime]:
    """
    Try to interpret an IR folder name like '20210904' (or the int 20210904)
    as a date. Returns a datetime at 00:00 or None.
    """
    # Handle None / NaN
    if folder_name is None:
        return None

    # Many ir_folder values are ints (e.g., 20150819), so force to string
    folder_str = str(folder_name).strip()
    if not folder_str:
        return None

    m = re.match(r"^(\d{4})(\d{2})(\d{2})$", folder_str)
    if not m:
        return None

    year, month, day = map(int, m.groups())
    try:
        return dt.datetime(year, month, day, 0, 0)
    except ValueError:
        return None



def best_timestamp_for_row(row: pd.Series) -> Optional[dt.datetime]:
    """
    Choose the best available timestamp for a row:
    1) event_timestamp_local (ISO string from filename) if valid
    2) ir_folder parsed as YYYYMMDD (00:00 time)
    """
    t1 = parse_iso_or_none(row.get("event_timestamp_local", ""))
    if t1 is not None:
        return t1

    t2 = parse_ir_folder_date(row.get("ir_folder", ""))
    return t2


# ---------------------------------------------------------------------------
# Geometry loading

def load_perimeter_from_path(path: Path) -> Optional[gpd.GeoDataFrame]:
    """
    Load perimeter geometries from a local path.

    - If path is a shapefile (.shp), read it directly.
    - If path is a file geodatabase (.gdb directory), read a suitable layer.
    - If path is a *directory that is NOT a .gdb*, look inside for a .shp
      (prefer names containing 'perim') and read that.

    Returns a GeoDataFrame (reprojected to EPSG:4326) or None if nothing usable is found.
    """
    if not path.exists():
        print(f"    ! path does not exist: {path}")
        return None

    # ------------------------------------------------------------------
    # Case 1: generic directory (like 20210723_Vinegar_shapefiles)
    if path.is_dir() and not path.name.lower().endswith(".gdb"):
        # serch for shapefiles inside this folder
        shapefiles = sorted(p for p in path.glob("*.shp"))
        if not shapefiles:
            print(f"    ! directory has no .shp files: {path}")
            return None

        # shapefiles whose name contains 'perim'/'perimeter'
        shp_perim = [p for p in shapefiles if "perim" in p.stem.lower()]
        chosen_shp = shp_perim[0] if shp_perim else shapefiles[0]

        print(f"    - found shapefile in dir {path.name}: {chosen_shp.name}")
        path = chosen_shp  # continue below as if user passed this .shp

    # ------------------------------------------------------------------
    # Case 2: Shapefile
    if path.suffix.lower() == ".shp":
        try:
            gdf = gpd.read_file(path)
            print(f"    - loaded shapefile with {len(gdf)} features: {path.name}")
        except Exception as e:
            print(f"    ! failed to read shapefile {path}: {e}")
            return None

    # ------------------------------------------------------------------
    # Case 3: File geodatabase (directory whose name ends as .gdb)
    elif path.is_dir() and path.name.lower().endswith(".gdb"):
        if fiona is None:
            print(f"    ! fiona not available, cannot read gdb {path}")
            return None

        try:
            layers = fiona.listlayers(path)
        except Exception as e:
            print(f"    ! failed to list layers in gdb {path}: {e}")
            return None

        perim_layers = [lyr for lyr in layers if "perim" in lyr.lower()]
        chosen_layer = None
        gdf = None

        candidate_layers = perim_layers if perim_layers else list(layers)
        for lyr in candidate_layers:
            try:
                tmp = gpd.read_file(path, layer=lyr)
                if not tmp.empty and tmp.geom_type.isin(["Polygon", "MultiPolygon"]).any():
                    gdf = tmp
                    chosen_layer = lyr
                    break
            except Exception as e:
                print(f"    ! failed to read layer {lyr} in {path}: {e}")
                continue

        if gdf is None:
            print(f"    ! no usable polygon layer found in gdb {path}")
            return None
        else:
            print(f"    - using gdb layer '{chosen_layer}' in {path.name}")

    else:
        print(f"    ! unsupported path type (not .shp or .gdb dir): {path}")
        return None

    # ------------------------------------------------------------------
    # Clean up geometry and reproject
    if gdf.empty:
        return None

    gdf = gdf[gdf.geometry.notna()].copy()
    if gdf.empty:
        return None

    try:
        if gdf.crs is not None and gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs(epsg=4326)
    except Exception:
        # If CRS is missing or invalid, assume it's already lon/lat
        pass

    return gdf


# ---------------------------------------------------------------------------
# Custom HTML legend

class TimeLegend(MacroElement):
    """
    Custom HTML legend showing a horizontal Inferno-like gradient
    with min/max datetimes on each side.
    """
    _template = Template("""
        {% macro script(this, kwargs) %}
        var legend = L.control({position: 'bottomright'});

        legend.onAdd = function (map) {
            var div = L.DomUtil.create('div', 'fire-legend');
            div.innerHTML = `
            <div style="
                background-color: rgba(0, 0, 0, 0.7);
                padding: 8px 10px;
                border-radius: 6px;
                color: #ffffff;
                font-size: 12px;
                box-shadow: 0 0 6px rgba(0,0,0,0.4);
            ">
              <div style="font-weight: 600; margin-bottom: 2px;">
                {{ this.title }}
              </div>
              {% if this.subtitle %}
              <div style="margin-bottom: 4px;">
                {{ this.subtitle }}
              </div>
              {% endif %}
              <div style="
                  width: 200px;
                  height: 10px;
                  background: linear-gradient(to right, {{ this.gradient }});
                  border-radius: 4px;
                  margin: 6px 0;
              "></div>
              <div style="
                  display: flex;
                  justify-content: space-between;
                  font-size: 11px;
              ">
                  <span style="white-space: nowrap;">{{ this.min_label }}</span>
                  <span style="white-space: nowrap;">{{ this.max_label }}</span>
              </div>
            </div>`;
            return div;
        };

        legend.addTo({{ this._parent.get_name() }});
        {% endmacro %}
    """)

    def __init__(
        self,
        title: str,
        subtitle: str,
        min_label: str,
        max_label: str,
        gradient: str,
    ):
        super().__init__()
        self._name = "TimeLegend"
        self.title = title
        self.subtitle = subtitle
        self.min_label = min_label
        self.max_label = max_label
        self.gradient = gradient


# ---------------------------------------------------------------------------
# Fire progression map (all perimeters, hoverable)

def build_fire_progression_map(
    data_root: Path,
    index_csv: Path,
    year: int,
    incident_query: str,
    out_html: Path,
    max_snapshots: Optional[int] = None,
) -> folium.Map:
    """
    Build a fire progression map for a single incident and save as HTML.

    - All perimeters are drawn at once (no time slider).
    - Color encodes time since first perimeter (Inferno colormap).
    - Hovering a polygon shows the perimeter datetime and highlights it.
    """
    data_root = data_root.resolve()
    index_csv = index_csv.resolve()
    out_html = out_html.resolve()

    print(f"data_root = {data_root}")
    print(f"index_csv = {index_csv}")
    print(f"year = {year}")
    print(f"incident_query = {incident_query}")

    df = pd.read_csv(index_csv)

    # Filter by year
    df_year = df[df["incident_year"].astype(str) == str(year)].copy()
    if df_year.empty:
        raise ValueError(f"No rows found for incident_year={year} in {index_csv}")

    # Filter by incident name (substring, case-insensitive)
    mask_inc = df_year["incident"].str.contains(incident_query, case=False, regex=False)
    df_inc = df_year[mask_inc].copy()

    if df_inc.empty:
        raise ValueError(
            f"No incidents in year {year} match query '{incident_query}'. "
            f"Available examples: {sorted(df_year['incident'].unique())[:10]}"
        )

    # If multiple distinct incidents match, keep all but report
    incidents_found = sorted(df_inc["incident"].unique())
    print(f"incidents matching '{incident_query}' in {year}: {incidents_found}")

    # Derive a usable timestamp column for ordering
    df_inc["ts"] = df_inc.apply(best_timestamp_for_row, axis=1)
    df_inc = df_inc.sort_values(["ts", "ir_folder", "file_name"])

    if max_snapshots is not None and max_snapshots > 0:
        df_inc = df_inc.head(max_snapshots)

    if df_inc.empty:
        raise ValueError("No usable rows (after timestamp parsing / filtering).")

    # Load geometries for each snapshot
    features: List[Dict[str, Any]] = []
    all_centroids = []
    largest_poly_area = 0.0
    largest_poly_bounds = None  # (minx, miny, maxx, maxy) in lon/lat

    for _, row in df_inc.iterrows():
        local_rel = row["local_path"]
        src_path = data_root / local_rel

        print(f"\n[snapshot] {row['incident']} | folder={row['ir_folder']} | file={row['file_name']}")
        print(f"                 path={src_path}")

        gdf = load_perimeter_from_path(src_path)
        if gdf is None or gdf.empty:
            print("     ! no geometry loaded; skipping snapshot")
            continue

        # Compute areas in a projected CRS to find the largest polygon
        try:
            if gdf.crs is None:
                gdf_ll = gdf.set_crs(epsg=4326, allow_override=True)
            else:
                gdf_ll = gdf.to_crs(epsg=4326)
            gdf_merc = gdf_ll.to_crs(epsg=3857)  # meters

            for geom_merc, geom_ll in zip(gdf_merc.geometry, gdf_ll.geometry):
                if geom_merc is None or geom_ll is None:
                    continue
                area = geom_merc.area  # m²
                if area > largest_poly_area:
                    largest_poly_area = area
                    largest_poly_bounds = geom_ll.bounds
        except Exception as e:
            print(f"    ! failed to compute area for largest polygon: {e}")

        #  timestamp
        ts = row["ts"]
        if ts is None:
            ts = parse_ir_folder_date(str(row["ir_folder"])) or dt.datetime(year, 1, 1)

        ts_iso = ts.isoformat()

        # Track centroid for map centering fallback
        try:
            centroid = gdf.to_crs(epsg=4326).geometry.unary_union.centroid
            all_centroids.append((centroid.y, centroid.x))
        except Exception:
            pass

        for _, feat_row in gdf.iterrows():
            geom = feat_row.geometry
            if geom is None:
                continue

            feature = {
                "type": "Feature",
                "geometry": mapping(geom),
                "properties": {
                    "time": ts_iso,
                    "incident": row["incident"],
                    "ir_folder": row["ir_folder"],
                    "file_name": row["file_name"],
                },
            }
            features.append(feature)

    if not features:
        raise ValueError("No geometries loaded for this incident; nothing to map.")

    # ------------------------------------------------------------
    # Compute elapsed time for each snapshot (for color mapping)
    real_times_dt = sorted({
        parse_iso_or_none(f["properties"]["time"])
        for f in features
        if f["properties"].get("time")
    })
    real_times_dt = [t for t in real_times_dt if t is not None]

    if real_times_dt:
        t0 = min(real_times_dt)
        t1 = max(real_times_dt)

        t0_str = t0.strftime("%Y-%m-%d %H:%M")
        t1_str = t1.strftime("%Y-%m-%d %H:%M")

        elapsed_map: Dict[str, float] = {}
        for t in real_times_dt:
            elapsed = (t - t0).total_seconds()
            elapsed_map[t.isoformat()] = elapsed

        for f in features:
            real_t = f["properties"]["time"]
            f["properties"]["elapsed"] = elapsed_map.get(real_t, 0.0)

        elapsed_values = [f["properties"]["elapsed"] for f in features]
        min_elapsed = min(elapsed_values)
        max_elapsed = max(elapsed_values) if max(elapsed_values) > 0 else 1.0
    else:
        min_elapsed, max_elapsed = 0.0, 1.0
        for f in features:
            f["properties"]["elapsed"] = 0.0
        t0_str = "unknown"
        t1_str = "unknown"

    # ------------------------------------------------------------
    # Colormap + styling and display_time for hover
    incident_label = incidents_found[0] if incidents_found else incident_query

    inferno_colors = [
        "#000004",
        "#320A5A",
        "#781C6D",
        "#BB3754",
        "#ED6925",
        "#FDB42F",
        "#FCFFA4",
    ]

    cmap = cm.LinearColormap(
        colors=inferno_colors,
        vmin=min_elapsed,
        vmax=max_elapsed,
    )

    for f in features:
        real_t = f["properties"]["time"]
        dt_obj = parse_iso_or_none(real_t)
        if dt_obj is not None:
            f["properties"]["display_time"] = dt_obj.strftime("%Y-%m-%d %H:%M")
        else:
            f["properties"]["display_time"] = str(real_t)

        elapsed = f["properties"].get("elapsed", 0.0)
        color = cmap(elapsed)

        f["properties"]["style"] = {
            "color": color,
            "weight": 1,
            "fillColor": color,
            "fillOpacity": 0.3,
        }

    # Draw order so oldest perimeters are ON TOP:
    features.sort(
        key=lambda f: f["properties"].get("elapsed", 0.0),
        reverse=True,  # newest first → oldest last → oldest on top
    )

    geojson = {
        "type": "FeatureCollection",
        "features": features,
    }

    # ------------------------------------------------------------
    # Build map, legend, and hoverable polygons
    if all_centroids:
        lat = sum(c[0] for c in all_centroids) / len(all_centroids)
        lon = sum(c[1] for c in all_centroids) / len(all_centroids)
        center = [lat, lon]
    else:
        center = [40.0, -115.0]

    print(f"Map center (fallback): {center}, snapshots: {len(features)}")

    m = folium.Map(
        location=center,
        zoom_start=9,
        tiles=None,
        control_scale=True,
    )

    folium.TileLayer(
        tiles=(
            "https://server.arcgisonline.com/ArcGIS/rest/services/"
            "World_Imagery/MapServer/tile/{z}/{y}/{x}"
        ),
        attr=(
            "Tiles © Esri — Source: Esri, i-cubed, USDA, USGS, AEX, "
            "GeoEye, Getmapping, Aerogrid, IGN, IGP, UPR-EGP, and the GIS User Community"
        ),
        name="Esri Satellite",
        overlay=False,
        control=True,
    ).add_to(m)

    gradient_str = ",".join(inferno_colors)
    legend = TimeLegend(
        title=f"{incident_label} ({year}) — IR perimeter datetime",
        subtitle="Inferred from IR perimeters",
        min_label=t0_str,
        max_label=t1_str,
        gradient=gradient_str,
    )
    m.add_child(legend)

    hover_tooltip = GeoJsonTooltip(
        fields=["display_time"],
        aliases=["IR perimeter:"],
        sticky=True,
    )

    folium.GeoJson(
        data=geojson,
        style_function=lambda feature: feature["properties"]["style"],
        highlight_function=lambda feature: {
            **feature["properties"]["style"],
            "fillOpacity": 1.0,
            "weight": 2,
        },
        tooltip=hover_tooltip,
        name="IR perimeters",
    ).add_to(m)

    if largest_poly_bounds is not None:
        minx, miny, maxx, maxy = largest_poly_bounds
        print(
            f"Largest polygon area ≈ {largest_poly_area:.2f} m², "
            f"bounds={largest_poly_bounds}"
        )
        m.fit_bounds([[miny, minx], [maxy, maxx]])

    out_html.parent.mkdir(parents=True, exist_ok=True)
    m.save(str(out_html))
    print(f"[done] Saved progression map to: {out_html}")
    return m


# ---------------------------------------------------------------------------
# CLI entrypoint (map)

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a fire progression map for a single incident."
    )
    parser.add_argument(
        "--data-root",
        required=True,
        help="Root directory used by scraper.py (where local_path is rooted).",
    )
    parser.add_argument(
        "--index",
        required=True,
        help="Path to CSV index produced by scraper.py (e.g., ir_events_index.csv).",
    )
    parser.add_argument(
        "--year",
        type=int,
        required=True,
        help="Incident year (e.g., 2021).",
    )
    parser.add_argument(
        "--incident",
        required=True,
        help="Incident name or substring to match (case-insensitive).",
    )
    parser.add_argument(
        "--out-html",
        required=True,
        help="Output HTML file for the map.",
    )
    parser.add_argument(
        "--max-snapshots",
        type=int,
        default=None,
        help="Optional maximum number of snapshots to include (for testing).",
    )

    args = parser.parse_args()

    data_root = Path(args.data_root)
    index_csv = Path(args.index)
    out_html = Path(args.out_html)

    build_fire_progression_map(
        data_root=data_root,
        index_csv=index_csv,
        year=args.year,
        incident_query=args.incident,
        out_html=out_html,
        max_snapshots=args.max_snapshots,
    )


if __name__ == "__main__":
    main()
