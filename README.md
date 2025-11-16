# salhab

Wildfire infrared (IR) perimeter **scraping, indexing, and visualization** for Great Basin incidents (2015–2025).

This repo contains:

- Python modules to **download** IR geodatabases/shapefiles from the NIFC FTP
- Utilities to **index** incidents and IR days
- Notebooks to **visualize daily fire progression** as interactive maps

---

## Repository structure

```text
salhab/
  modules/
    scraper.py              # download and extract IR geodatabases / shapefile bundles
    request_fire.py         # calling external fire simulation API
    fire_progression_map.py # progression maps from indexed IR perimeters

  notebooks/
    fire_progression_viz.ipynb          # single-incident fire progression visualization
    fire_progression_viz_select.ipynb   # UI-based incident selection + map generation

  data/
    great_basin_ir_2015_2025/  # local copy of IR data organized by year/incident
    ir_events_index.csv        # index of incidents, years, and available IR days (generated upstream)

  .gitignore
  README.md   # ← this file
