import argparse
import json

import geopandas as gpd
import matplotlib.pyplot as plt
import requests

API_SIMULATE   = "https://api.testpty.xyz/simulate"
API_OUTPUT_ROOT = "https://api.testpty.xyz/output"


def call_api(api_key: str) -> dict:
    payload = {
        "config": {
            "lon": 32.94,
            "lat": 35.01,
            "start_time": "2019-06-19T12:00:00",
            "duration": 32000,
            "features": {"spotting": True, "crown_fire": False, "wui": False},
        }
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    r = requests.post(API_SIMULATE, json=payload, headers=headers, timeout=3000)
    r.raise_for_status()

    data = r.json()
    if data.get("status") != "success":
        raise RuntimeError(f"API said status={data.get('status')}\n{json.dumps(data, indent=2)[:400]}")

    return data


def pick_and_load_perimeter(run_json: dict) -> gpd.GeoDataFrame:
    outs = run_json["outputs"]

    if run_json["outputs"].get("perimeters"):
        perim = run_json["outputs"]["perimeters"][-1]
        print(f"[i] using embedded perimeter: {perim['file']}")
        return gpd.GeoDataFrame.from_features(perim["data"]["features"])

    files = [f for f in outs.get("files", []) if f["name"].lower().endswith(".geojson")]
    if not files:
        raise RuntimeError("No perimeter GeoJSON in 'perimeters' or 'files' list!")

    chosen = files[0]["name"]
    exec_id = run_json["execution_id"]
    url = f"{API_OUTPUT_ROOT}/{exec_id}/{chosen}"
    print(f"[i] downloading {url}")

    r = requests.get(url, timeout=120)
    r.raise_for_status()

    return gpd.read_file(r.text if isinstance(r.text, str) else r.content)


def plot_perimeter(gdf: gpd.GeoDataFrame, title: str = "fire perimeter"):
    ax = gdf.plot(edgecolor="black", facecolor="none", figsize=(6, 6))
    ax.set_title(title)
    ax.set_aspect("equal", adjustable="datalim")
    ax.set_xlabel("longitude")
    ax.set_ylabel("latitude")
    plt.tight_layout()
    plt.show()




if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", required=True, help="FIRE_API_KEY")
    args = ap.parse_args()

    run = call_api(args.key)
    gdf = pick_and_load_perimeter(run)
    plot_perimeter(gdf, title=run["execution_id"])
