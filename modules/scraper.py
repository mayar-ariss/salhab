#!/usr/bin/env python3
"""
scraper.py

Author: Mayar Ariss, MIT Senseable City Lab
Created: 2025-11-15

Scrape IR geodatabases (.gdb / .gdb.zip) and IR shapefile bundles from:

  https://ftp.wildfire.gov/public/incident_specific_data/

For each fire season folder 2015_Incidents ... 2025_Incidents:
  - Enters each incident folder
  - Enters the IR/ folder (if present)
  - Walks all IR date subfolders (e.g., 20150819/, 20250804_UTF_Tech/)
  - Downloads any archive/file relevant to IR geodatabases or shapefiles:
      - .gdb in the name (fro ex *.gdb.zip)
      - zips whose names contain "Shapefile"/"Shapefiles" (e.g. *_IR_Shapefiles.zip, *ShapeFileOutputs.zip)
  - For shapefile zips:
      - Only extracts shapefile groups whose basename contains "perimeter" or "perim"
      - Disregards all other layers
  - Extracts:
      - File geodatabases (.gdb directories)
      - Perimeter shapefiles (.shp + associated sidecar files)
  - By default, deletes the original .zip to save space
  - Removes empty IR day / IR / incident folders created locally
  - Records metadata (year, incident, IR folder, filename, URL, local path, timestamp) in a CSV index

example:

  python scraper.py \
      --out ./great_basin_ir_2015_2025 \
      --index ir_events_index.csv
      
"""

# ensure required packages are installed (requests, beautifulsoup4)
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

for _pkg in ("requests", "beautifulsoup4"):
    ensure_package(_pkg)
# -------------------------------------------------------------------------

import argparse
import csv
import datetime as dt
import os
import re
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Dict, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# Parent directory (you can check https://ftp.wildfire.gov/public/incident_specific_data to select specific incident areas).
DEFAULT_BASE_URL = (
    "https://ftp.wildfire.gov/public/incident_specific_data/great_basin/"
)

LISTING_TIMEOUT = 30
DOWNLOAD_TIMEOUT = 300
SLEEP_BETWEEN_REQUESTS = 0.0  # 0 for max speed; raise if you want to throttle

# Match 2015_Incidents ... 2025_Incidents (case-insensitive; handles 2025_incidents)
SEASON_PATTERN = re.compile(r"^(20(1[5-9]|2[0-5]))_incidents$", re.IGNORECASE)

session = requests.Session()
session.headers.update(
    {
        "User-Agent": "mayar-ir-gdb-scraper/0.5 (contact: your-email@example.com)"
    }
)


def fetch_html(url: str) -> str:
    """Get directory listing HTML."""
    resp = session.get(url, timeout=LISTING_TIMEOUT)
    resp.raise_for_status()
    return resp.text


def list_dirs_files(url: str) -> Tuple[List[Tuple[str, str]], List[Tuple[str, str]]]:
    """
    Parse Apache-style directory listing page.

    Returns:
        - dirs:  list of (name, absolute_url) for subdirectories
        - files: list of (name, absolute_url) for files
    """
    html = fetch_html(url)
    soup = BeautifulSoup(html, "html.parser")

    dirs: List[Tuple[str, str]] = []
    files: List[Tuple[str, str]] = []

    for a in soup.find_all("a"):
        href = a.get("href")
        name = (a.text or "").strip()

        if not href:
            continue

        if href.startswith("?") or href.startswith("#"):  # skip query links and anchors
            continue

        if href.startswith("../") or "Parent Directory" in name: #skip "Parent Directory"
            continue

        if href.endswith("/"):
            abs_url = urljoin(url, href)
            dirs.append((name, abs_url))
        else:
            abs_url = urljoin(url, href)
            files.append((name, abs_url))

    return dirs, files


def parse_ir_timestamp(filename: str) -> dt.datetime | None:
    """
    Parse the IR overflight timestamp from filenames as:

        20250731_2202_Cedar_IR.gdb.zip
        20250809_2305_Beulah_IR.gdb
        20150819_0248_Bobcat_IR_Shapefiles.zip
        20216010_0128_Bear_IR_Shapefiles.zip  (typo in month -> auto-correct)

    Includes a small fix for swapped month digits (e.g. "60" -> "06" for june).
    """
    pattern = r"(?P<date>\d{8})_(?P<time>\d{4})_.*?_IR(?:_.*)?\.(?:gdb(?:\.zip)?|zip)$"
    m = re.search(pattern, filename)
    if not m:
        return None

    d_str = m.group("date")
    t_str = m.group("time")

    year = int(d_str[0:4])
    month_str = d_str[4:6]
    day = int(d_str[6:8])
    hour = int(t_str[0:2])
    minute = int(t_str[2:4])

    # try the straightforward datetime construction
    try:
        month = int(month_str)
        return dt.datetime(year, month, day, hour, minute)
    except ValueError:
        pass  # fall through to typo correction

    #try swapped month digits (e.g. "60" -> "06")
    try:
        swapped_month_str = month_str[::-1]  # "60" -> "06"
        swapped_month = int(swapped_month_str)
        if 1 <= swapped_month <= 12:
            return dt.datetime(year, swapped_month, day, hour, minute)
    except ValueError:
        # still invalid (eg., day out of range)
        return None

    # if we still can't make sense of it, give up 
    return None


def download_file(url: str, dest: Path) -> None:
    """Stream-download a file to dest (atomic write via .part)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")

    print(f"      -> downloading {url} -> {dest}")
    with session.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                if not chunk:
                    continue
                f.write(chunk)

    tmp.replace(dest)
    if SLEEP_BETWEEN_REQUESTS > 0:
        time.sleep(SLEEP_BETWEEN_REQUESTS)


def extract_relevant_from_zip(
    zip_path: Path,
    target_dir: Path,
    keep_zip: bool = False,
    perimeter_only: bool = False,
) -> Dict[str, List[Path]]:
    """
    Extract contents of zip_path into target_dir and return:

        {
          "gdb": [Path to .gdb directory (if any)],
          "shp": [Paths to .shp files (if any)]
        }

    Behaviour:
      - If perimeter_only is False:
          - Extracts all members.
      - If perimeter_only is True (shapefile zips):
          - Only extracts members whose basename contains "perimeter" or "perim".
            (e.g., fire_perimeter.shp, perimeter.dbf, perim_20250801.shx, etc.)
          - Disregards all other files/layers.

    If keep_zip is False, deletes the .zip file after extraction.
    """
    result: Dict[str, List[Path]] = {"gdb": [], "shp": []}

    if not zip_path.suffix.lower().endswith("zip"):
        return result

    print(f"        • extracting {zip_path.name}")
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            members = [m for m in zf.namelist() if m.strip()]

            if perimeter_only:
                members_to_extract = []
                for m in members:
                    basename = m.split("/")[-1].lower()
                    if "perimeter" in basename or "perim" in basename:
                        members_to_extract.append(m)
            else:
                members_to_extract = members

            # If nothing to extract (e.g., no perimeter shapefiles), bail early
            if not members_to_extract:
                members_to_scan: List[str] = []
            else:
                zf.extractall(target_dir, members=members_to_extract)
                members_to_scan = members_to_extract

        # Identify .gdb directories + .shp files from extracted members
        gdb_dirs = set()
        shp_relpaths: List[str] = []

        for member in members_to_scan:
            m = member.rstrip("/")
            if not m:
                continue

            parts = m.split("/")
            top = parts[0]

            # file geodatabase folder
            if top.lower().endswith(".gdb"):
                gdb_dirs.add(top)

            #Shapefiles anywhere (only perimeter ones were extracted when perimeter_only=True)
            if parts[-1].lower().endswith(".shp"):
                shp_relpaths.append(m)

        for d in gdb_dirs:
            result["gdb"].append(target_dir / d)

        for rel in shp_relpaths:
            result["shp"].append(target_dir / rel)

    except zipfile.BadZipFile:
        print(f"        ! Bad zip file, skipping: {zip_path}")
    finally:
        if not keep_zip:
            try:
                zip_path.unlink()
                print(f"        • deleted zip {zip_path.name}")
            except OSError:
                print(f"        ! could not delete zip {zip_path.name}")

    return result


def process_ir_file(
    fname: str,
    f_url: str,
    day_dir: Path,
    out_root: Path,
    season_clean: str,
    incident_year: str,
    incident_name: str,
    date_folder_clean: str,
    keep_zip: bool,
) -> Dict[str, str] | None:
    """
    Download + extract one IR file (gdb or shapefile zip), and
    return a single index row or None if not relevant or not perimeter.
    """
    lname = fname.lower()
    is_zip = lname.endswith(".zip")
    base = lname[:-4] if is_zip else lname  # name without .zip

    # IR geodatabases (unpacked or zipped) 
    # Catch:
    #   -.gdb
    #   -_IRgdb.zip
    #   -_IR_gdb.zip
    is_gdb_related = (
        ".gdb" in base
        or "irgdb" in base          
        or "ir_gdb" in base 
        )        

    # IR shapefile bundles (zips) 
    # Catch:
    #   -_IR_Shapefiles.zip, -_IR_Shapefile.zip
    #   -_IR_shps.zip, -_IR_shp.zip, etc.
    is_shapefile_zip = (
        is_zip
        and (
            "shapefile" in base
            or "shapefiles" in base
            or "shps" in base
            or "shp" in base
        )
    )

    # Generic IR zips (e.g. 20150819_Cougar_IR.zip)
    has_ir_token = (
        "_ir" in base
        or "ir_" in base
        or base.endswith("ir")
    )
    is_ir_zip_generic = is_zip and has_ir_token and not (is_gdb_related or is_shapefile_zip)

    # If it's neither gdb-related nor shapefile nor a generic IR.zip, ignore 
    if not (is_gdb_related or is_shapefile_zip or is_ir_zip_generic):
        return None

    local_archive_path = day_dir / fname

    if local_archive_path.exists():
        print(f"        = already exists, skipping download {fname}")
    else:
        download_file(f_url, local_archive_path)

    local_path_for_index: Path = local_archive_path

    if is_zip:
    
        perimeter_only = is_shapefile_zip or is_ir_zip_generic

        extracted = extract_relevant_from_zip(
            local_archive_path,
            day_dir,
            keep_zip=keep_zip,
            perimeter_only=perimeter_only,
        )

        # GDB logic: keep full .gdb folders
        if extracted["gdb"]:
            local_path_for_index = extracted["gdb"][0]
        elif extracted["shp"]:
            # Perimeter shapefile (or the first of them)
            local_path_for_index = extracted["shp"][0]
        else:
            # For shapefile/generic IR zips in perimeter-only mode:
            #   if nothing relevant was extracted (no perimeter shapefile),
            #   treat this file as non-event and do NOT add an index row.
            if perimeter_only:
                return None
            # For other zips (pure gdb) fall back to IR day directory
            local_path_for_index = day_dir

    # Parse timestamp from filename (if in IR naming format)
    ts = parse_ir_timestamp(fname)
    ts_iso = ts.isoformat() if ts is not None else ""

    # incident_base: part after the first underscore, if present
    incident_base = (
        incident_name.split("_", 1)[-1]
        if "_" in incident_name
        else incident_name
    )

    return {
        "incident_year": incident_year,
        "season_dir": season_clean,
        "incident": incident_name,
        "incident_base": incident_base,
        "ir_folder": date_folder_clean,
        "file_name": fname,
        "file_url": f_url,
        "local_path": str(local_path_for_index.relative_to(out_root)),
        "event_timestamp_local": ts_iso,
    }



def _is_empty_dir(path: Path) -> bool:
    """Return True if path exists and is an empty directory."""
    if not path.exists() or not path.is_dir():
        return False
    try:
        next(path.iterdir())
        return False
    except StopIteration:
        return True


def scrape_ir_gdb_and_shapefiles(
    base_url: str,
    out_root: Path,
    keep_zip: bool = False,
    max_workers: int = -1,
    ) -> List[Dict[str, str]]:
    """
    Main scraper logic.

    Returns:
        List of index rows (dicts) to later write into CSV.
    """

    if max_workers is None or max_workers <= 0:
        cpu_count = os.cpu_count() or 4

        max_workers = min(32, cpu_count * 4)
    print(f"[config] Using max_workers={max_workers}")

    out_root.mkdir(parents=True, exist_ok=True)
    index_rows: List[Dict[str, str]] = []

    print(f"[base] {base_url}")
    seasons, _ = list_dirs_files(base_url)

    # Loop over 2015_Incidents ... 2025_Incidents
    for season_name, season_url in seasons:
        season_clean = season_name.rstrip("/")
        if not SEASON_PATTERN.match(season_clean):
            continue

        incident_year = season_clean.split("_", 1)[0]
        print(f"\n[season] {season_clean} (year={incident_year})")

        incidents, _ = list_dirs_files(season_url)

        for incident_name, incident_url in incidents:
            incident_name = incident_name.rstrip("/")
            print(f"\n  [incident] {incident_name}")

            # Find IR/ subdirectory for this incident before creating any folder
            subdirs, _ = list_dirs_files(incident_url)
            ir_candidates = [d for d in subdirs if d[0].rstrip("/") == "IR"]

            if not ir_candidates:
                print("    - no IR/ directory; skipping (no local folders created)")
                continue

            _, ir_url = ir_candidates[0]
            print(f"    - IR directory: {ir_url}")

            # List all IR subfolders and keep only those starting with 8 digits
            raw_date_dirs, _ = list_dirs_files(ir_url)

            normalized_date_entries = []
            for raw_name, date_url in raw_date_dirs:
                raw_clean = raw_name.rstrip("/")
                m = re.match(r"(\d{8})", raw_clean)
                if not m:
                    print(f"    - skipping non-date IR subfolder {raw_clean}")
                    continue
                day_key = m.group(1)  # YYYYMMDD
                normalized_date_entries.append((raw_clean, day_key, date_url))

            # Use unique day keys to count IR "days"
            unique_days = sorted({day_key for _, day_key, _ in normalized_date_entries})
            num_ir_days = len(unique_days)

            # Apply the ≥ 12 IR days filter
            if num_ir_days < 12:
                print(
                    f"    - IR/ has only {num_ir_days} distinct date folders (< 12); "
                    "skipping incident"
                )
                continue

            # Only now create the local incident directory
            incident_dir = out_root / season_clean / incident_name
            incident_dir.mkdir(parents=True, exist_ok=True)

            for raw_name, day_key, date_url in normalized_date_entries:
                # raw_name is the remote folder name
                # day_key is the normalized local folder: YYYYMMDD
                print(f"    [IR day] {raw_name} → {day_key}")
                day_dir = incident_dir / "IR" / day_key

                _, files = list_dirs_files(date_url)

                # Use a thread pool to download/extract all IR files for this day in parallel
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = [
                        executor.submit(
                            process_ir_file,
                            fname,
                            f_url,
                            day_dir,
                            out_root,
                            season_clean,
                            incident_year,
                            incident_name,
                            day_key,      # <- use normalized YYYYMMDD in the index
                            keep_zip,
                        )
                        for fname, f_url in files
                    ]

                    for fut in as_completed(futures):
                        row = fut.result()
                        if row is not None:
                            index_rows.append(row)

                # After processing this day, if the day_dir exists but is empty, remove it
                if _is_empty_dir(day_dir):
                    day_dir.rmdir()
                    print(f"    - removed empty IR day folder {day_dir}")

            # After all days, optionally remove empty IR/ and incident folders
            ir_root_dir = incident_dir / "IR"
            if _is_empty_dir(ir_root_dir):
                ir_root_dir.rmdir()
                print(f"  - removed empty IR folder {ir_root_dir}")

            if _is_empty_dir(incident_dir):
                incident_dir.rmdir()
                print(f"  - removed empty incident folder {incident_dir}")

    return index_rows


def write_index_csv(rows: List[Dict[str, str]], path: Path) -> None:
    """Write the collected rows into a CSV index."""
    if not rows:
        print("No rows to write; index CSV not created.")
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys())

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\n[index] wrote {len(rows)} rows to {path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Scrape IR geodatabases and IR perimeter shapefile bundles "
            "from NIFC Great Basin 2015–2025 incidents (parallel downloads)."
        )
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=(
            "Parent directory for Great Basin datasets "
            "(default: https://ftp.wildfire.gov/public/incident_specific_data/great_basin/)."
        ),
    )
    parser.add_argument(
        "--out",
        default="great_basin_ir_2015_2025",
        help="Output root directory for downloads.",
    )
    parser.add_argument(
        "--index",
        default="ir_events_index.csv",
        help="Name of CSV file (inside --out) for event index.",
    )
    parser.add_argument(
        "--keep-zip",
        action="store_true",
        help="If set, keep downloaded .zip files after extraction (default: delete them).",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=-1,
        help=(
            "Maximum number of parallel download workers. "
            "Use -1 to auto-select based on CPU count (default: -1)."
        ),
    )

    args = parser.parse_args()

    out_root = Path(args.out).resolve()
    index_path = out_root / args.index

    rows = scrape_ir_gdb_and_shapefiles(
        args.base_url,
        out_root,
        keep_zip=args.keep_zip,
        max_workers=args.max_workers,
    )
    write_index_csv(rows, index_path)


if __name__ == "__main__":
    main()
