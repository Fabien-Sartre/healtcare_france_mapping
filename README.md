# Accès aux soins en France

Interactive map of healthcare access in France, built entirely from open government data. It answers one question, for any of 14 health professions:

> Where is access to a doctor / dentist / physiotherapist / … the weakest, and where would a new practitioner serve the most under-served residents?

**Live site:** https://fabien-sartre.github.io/healthcare-france-mapping/

The site is fully static — all analysis is precomputed offline and served as JSON from GitHub Pages. No backend, no API keys.

## What it does

- **Diagnostic** — for each populated grid cell, the local density of practitioners (per 100,000 residents within a 15 km catchment), compared to the national average of the profession. Basins are ranked two ways: most residents in low-density areas, and lowest density per resident. Each ranked basin shows how many practitioners are missing to bring it back above the low-density threshold. Clicking anywhere on the map studies that location: "1 practitioner per N residents" in the chosen radius, compared to the national and departmental averages, the number of practitioners missing, and the effect of one additional practitioner settling there.
- **Population** — 3D view of where people live: the demand side that the supply has to cover.
- **Offre de soins** — health facilities (FINESS), filterable by legal status and category.
- **Méthode** — what is measured, and the stated limits of the approach.

## Method

For every population grid cell, practitioners of a given profession within a 15 km straight-line radius (a reproducible proxy for roughly 15 minutes of travel) are counted and divided by the population of the same radius, giving a local density per 100,000 residents. A zone is considered low-density ("faible densité") below 70 % of the profession's national average.

The approach is inspired by the DREES APL indicator (accessibilité potentielle localisée) without substituting for it. Known limits, also stated on the site: straight-line distance ignores the road network; RPPS counts practitioners without weighting by activity; professions are counted at the profession level (e.g. "Médecin" includes both GPs and specialists — consistent on both sides of the comparison).

## Architecture

```
data/download_data.py   downloads ~200 MB of open data (RPPS, INSEE, FINESS)
        │                and writes data/processed/*.parquet   (gitignored)
        ▼
data/export_static.py   converts parquet → compact JSON for the browser
        ▼
data/build_access.py    computes the analysis layers: local density per point,
        │                ranked priority basins, headline KPIs per profession
        ▼
docs/                   static site (vanilla JS, deck.gl + MapLibre GL),
                         published with GitHub Pages
```

The heavy work (pairwise haversine over every population point × commune, for 14 professions) is vectorised with NumPy and runs in minutes; the browser only renders precomputed JSON.

## Data sources

| Source | Use |
|--------|-----|
| [RPPS / Annuaire santé](https://www.data.gouv.fr/fr/datasets/annuaire-sante-professionnels-de-sante/) (data.gouv.fr) | Registered health professionals, by commune and profession |
| INSEE via [geo.api.gouv.fr](https://geo.api.gouv.fr) | Commune reference, coordinates, population |
| [FINESS](https://www.data.gouv.fr/) (data.gouv.fr) | Health facilities |
| IGN / [france-geojson](https://github.com/gregoiredavid/france-geojson) | Administrative boundaries |

All data is open and regularly updated by French public services. Scope: France métropolitaine.

## Run locally

View the site (the analysis JSON is committed, nothing to build):

```bash
python -m http.server 8000 --directory docs
```

Rebuild the data from scratch:

```bash
uv sync
python data/download_data.py    # ~200 MB download → data/processed/
python data/export_static.py    # parquet → docs/data/*.json
python data/build_access.py     # analysis layers → docs/data/*.json
```

## Project structure

```
├── data/
│   ├── download_data.py    # fetch raw open data → parquet (gitignored)
│   ├── export_static.py    # parquet → compact JSON for the site
│   └── build_access.py     # density analysis, priority basins, KPIs
├── docs/                   # static site, served by GitHub Pages
│   ├── index.html
│   ├── js/                 # app shell + one module per view
│   ├── css/
│   └── data/               # precomputed JSON consumed by the site
```
