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
    scraper.py              # Download and extract IR geodatabases / shapefile bundles
    request_fire.py         # Helper(s) for calling external fire simulation APIs (WIP/optional)
    fire_progression_map.py # Build interactive progression maps from indexed IR perimeters

  notebooks/
    fire_progression_viz.ipynb          # Single-incident fire progression visualization
    fire_progression_viz_select.ipynb   # UI-based incident selection + map generation

  data/
    great_basin_ir_2015_2025/  # (Optional, large) Local copy of IR data organized by year/incident
    ir_events_index.csv        # Index of incidents, years, and available IR days (generated upstream)

  .gitignore
  README.md   # ← this file
