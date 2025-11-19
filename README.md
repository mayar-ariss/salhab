# salhab

Wildfire infrared (IR) perimeter **scraping, indexing, and visualization** for US fire incidents (2015–2025), with hook to external physics-based wildfire simulation API.

This repo contains:

- A scraper to **download** IR geodatabases / shapefile bundles from the NIFC FTP and build a CSV index of usable perimeters. 
- A mapper to **build fire progression maps** for a given incident/year from the indexed IR perimeters. 
- A small client to **call external wildfire simulation API**, pull a perimeter GeoJSON, and plot it as a quick sanity check. 
- Notebooks to **browse incidents and generate interactive maps** from the indexed data.

---

## Repository structure

```text
salhab/
  modules/
    scraper.py              # scrape & download IR gdbs/shapefile bundles from NIFC FTP
                            
    fire_progression_map.py # build progression maps from the indexed IR perimeters
                            
    request_fire.py         # minimal client for external fire simulation API
                            

  notebooks/
    fire_progression_viz.ipynb          # single-incident fire progression visualization (from index)
    fire_progression_viz_select.ipynb   # UI-based incident/year selection + map generation

  data/
    great_basin_ir_2015_2025/           # local copy of IR data for Great Basin
      ir_events_index.csv               # index of incidents, IR folders, local paths & timestamps (written by scraper.py inside this directory)

  requirements.txt                      # Python dependencies
  .gitignore
  README.md                             # ← this file
