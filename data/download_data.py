#!/usr/bin/env python3
"""
Download open healthcare data from French government sources.

Data sources:
  1. Annuaire Santé — all registered healthcare professionals (CNAM / data.gouv.fr)
  2. Communes reference with GPS coordinates and population (geo.api.gouv.fr)
  3. Départements GeoJSON boundaries (france-geojson / GitHub)

Usage:
    python data/download_data.py
"""

import json
import os
import sys
from io import StringIO
from pathlib import Path

import pandas as pd
import requests

DATA_DIR = Path(__file__).resolve().parent
RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"

# Ensure directories exist
RAW_DIR.mkdir(parents=True, exist_ok=True)
PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

# ──────────────────────────────────────────────────────────────────────
# 1. Communes — lightweight reference with coordinates + population
# ──────────────────────────────────────────────────────────────────────

COMMUNES_API = "https://geo.api.gouv.fr/communes"
COMMUNES_PARAMS = {
    "fields": "nom,code,codesPostaux,centre,population,codeDepartement,codeRegion",
    "format": "json",
}


def download_communes() -> pd.DataFrame:
    """Fetch all French communes with centroid coordinates from geo.api.gouv.fr."""
    print("[1/6] Downloading communes from geo.api.gouv.fr ...")
    resp = requests.get(COMMUNES_API, params=COMMUNES_PARAMS, timeout=120)
    resp.raise_for_status()
    communes_json = resp.json()

    # Also fetch arrondissements for Paris/Lyon/Marseille so population
    # is spread across multiple points instead of one giant dot.
    print("   Fetching arrondissements (Paris/Lyon/Marseille) ...")
    arr_resp = requests.get(
        COMMUNES_API,
        params={**COMMUNES_PARAMS, "type": "arrondissement-municipal"},
        timeout=60,
    )
    arr_json = arr_resp.json() if arr_resp.status_code == 200 else []

    # Parent commune codes to replace with their arrondissements
    # (Paris=75056, Lyon=69123, Marseille=13055)
    PARENT_COMMUNE_CODES = {"75056", "69123", "13055"}
    parent_codes = PARENT_COMMUNE_CODES if arr_json else set()

    records = []
    for c in communes_json:
        centre = c.get("centre")
        if not centre:
            continue
        # Skip parent communes that have arrondissement detail
        if c["code"] in parent_codes:
            continue
        records.append(_commune_record(c))

    # Add arrondissements
    for c in arr_json:
        centre = c.get("centre")
        if not centre:
            continue
        records.append(_commune_record(c))

    df = pd.DataFrame(records)
    if len(df) < 30000:
        print(f"   !! WARNING: Only {len(df):,} communes downloaded (expected ~35k)")
    out = PROCESSED_DIR / "communes.parquet"
    df.to_parquet(out, index=False)
    print(f"   -> {len(df):,} communes saved to {out.name}")
    return df


def _commune_record(c: dict) -> dict:
    return {
        "code_commune": c["code"],
        "nom_commune": c["nom"],
        "codes_postaux": ",".join(c.get("codesPostaux", [])),
        "code_departement": c.get("codeDepartement", ""),
        "code_region": c.get("codeRegion", ""),
        "population": c.get("population", 0),
        "longitude": c["centre"]["coordinates"][0],
        "latitude": c["centre"]["coordinates"][1],
    }


# ──────────────────────────────────────────────────────────────────────
# 1b. IRIS zones — finer population grid (~49k zones vs ~35k communes)
# ──────────────────────────────────────────────────────────────────────

WFS_URL = "https://data.geopf.fr/wfs/ows"
IRIS_TYPENAME = "STATISTICALUNITS.IRIS.PE:contours_iris_pe"
WFS_PAGE_SIZE = 5000

# INSEE census population at IRIS level (2022)
INSEE_IRIS_POP_URL = (
    "https://www.insee.fr/fr/statistiques/fichier/8647014/"
    "base-ic-evol-struct-pop-2022_csv.zip"
)


def _download_insee_iris_population() -> pd.DataFrame:
    """Download actual IRIS-level population from INSEE census data (2022)."""
    import zipfile
    from io import BytesIO

    print("   Downloading INSEE IRIS population (census 2022) ...")
    resp = requests.get(INSEE_IRIS_POP_URL, timeout=120)
    resp.raise_for_status()

    with zipfile.ZipFile(BytesIO(resp.content)) as zf:
        csv_name = next((n for n in zf.namelist() if n.lower().endswith(".csv")), None)
        if csv_name is None:
            print("   !! No CSV found in INSEE IRIS zip")
            return pd.DataFrame()

        with zf.open(csv_name) as f:
            df = pd.read_csv(f, sep=";", encoding="latin-1", dtype=str, low_memory=False)

    df.columns = df.columns.str.strip()

    # Find population column: P22_POP (2022), P21_POP (2021), etc.
    pop_col = next(
        (c for c in df.columns if c.upper().startswith("P") and c.upper().endswith("_POP") and len(c) <= 7),
        None,
    )
    if pop_col is None or "IRIS" not in df.columns:
        print(f"   !! Missing expected columns. Available: {list(df.columns[:15])}")
        return pd.DataFrame()

    result = df[["IRIS", pop_col]].copy()
    result.columns = ["code_iris", "population"]
    result["code_iris"] = result["code_iris"].astype(str).str.strip()
    result["population"] = pd.to_numeric(result["population"], errors="coerce").fillna(0).astype(int)

    print(f"   INSEE: {len(result):,} IRIS rows, column {pop_col}")
    return result


HEX_RADIUS_M = 5000  # must match the HexagonLayer radius in the Streamlit page


def download_iris(communes_df: pd.DataFrame) -> pd.DataFrame:
    """
    Download IRIS geometries, join with INSEE population, then build a
    hex grid and compute each hex cell's population from the exact area
    overlap with IRIS polygons.

    hex_pop = sum( iris_pop × intersection_area / iris_area )

    All geometry work is done in Lambert-93 (EPSG:2154, metres) so
    hexagons are regular and area ratios are accurate.
    """
    import math
    import geopandas as gpd
    import numpy as np
    from shapely.geometry import shape, Polygon
    from shapely import STRtree

    print("[2/6] Downloading IRIS zones from IGN WFS ...")

    # ── 1. Paginated WFS download ────────────────────────────────────
    all_features = []
    offset = 0
    while True:
        resp = requests.get(
            WFS_URL,
            params={
                "SERVICE": "WFS",
                "VERSION": "2.0.0",
                "REQUEST": "GetFeature",
                "TYPENAMES": IRIS_TYPENAME,
                "COUNT": str(WFS_PAGE_SIZE),
                "STARTINDEX": str(offset),
                "OUTPUTFORMAT": "application/json",
            },
            timeout=120,
        )
        resp.raise_for_status()
        features = resp.json().get("features", [])
        all_features.extend(features)
        print(f"\r   Downloaded {len(all_features):,} IRIS zones ...", end="", flush=True)
        if len(features) < WFS_PAGE_SIZE:
            break
        offset += WFS_PAGE_SIZE
    print(f"\r   Downloaded {len(all_features):,} IRIS zones total")

    # ── 2. Build GeoDataFrame of IRIS polygons ───────────────────────
    iris_records = []
    iris_geoms = []
    skipped_geom = 0
    for feat in all_features:
        props = feat.get("properties", {})
        geom = feat.get("geometry")
        if not geom:
            continue
        try:
            shp = shape(geom)
            if shp.is_empty or not shp.is_valid:
                shp = shp.buffer(0)
        except Exception:
            skipped_geom += 1
            continue
        iris_records.append({
            "code_iris": props.get("code_iris", ""),
            "code_commune": props.get("code_insee", ""),
        })
        iris_geoms.append(shp)

    iris_gdf = gpd.GeoDataFrame(iris_records, geometry=iris_geoms, crs="EPSG:4326")
    print(f"   Built {len(iris_gdf):,} IRIS polygons")
    if skipped_geom:
        print(f"   !! Skipped {skipped_geom} features with geometry errors")

    # ── 3. Join INSEE population ─────────────────────────────────────
    insee_pop = _download_insee_iris_population()
    if not insee_pop.empty:
        iris_gdf["code_iris"] = iris_gdf["code_iris"].astype(str).str.strip()
        iris_gdf = iris_gdf.merge(insee_pop, on="code_iris", how="left")
        iris_gdf["population"] = iris_gdf["population"].fillna(0)
    elif not communes_df.empty:
        print("   !! INSEE unavailable, falling back to commune population")
        pop = communes_df[["code_commune", "population"]].copy()
        pop["code_commune"] = pop["code_commune"].astype(str).str.strip()
        iris_gdf["code_commune"] = iris_gdf["code_commune"].astype(str).str.strip()
        n_per = iris_gdf.groupby("code_commune").size().reset_index(name="_n")
        iris_gdf = iris_gdf.merge(n_per, on="code_commune", how="left")
        iris_gdf = iris_gdf.merge(pop, on="code_commune", how="left")
        iris_gdf["population"] = (iris_gdf["population"].fillna(0) / iris_gdf["_n"].fillna(1))
        iris_gdf = iris_gdf.drop(columns=["_n"])
    else:
        iris_gdf["population"] = 0

    # Restrict to mainland France
    iris_gdf = iris_gdf.cx[-5.5:10, 41:51.5]
    iris_gdf = iris_gdf[iris_gdf["population"] > 0].copy()
    print(f"   Mainland IRIS with pop > 0: {len(iris_gdf):,}")

    # ── 4. Project to Lambert-93 for accurate area computation ───────
    iris_gdf = iris_gdf.to_crs("EPSG:2154")
    iris_gdf["iris_area"] = iris_gdf.geometry.area  # m²

    # ── 5. Generate regular hex grid in Lambert-93 ───────────────────
    r = HEX_RADIUS_M  # hex radius in metres
    bounds = iris_gdf.total_bounds  # [minx, miny, maxx, maxy]

    # Flat-top hex tiling (equal spacing in metres — no distortion)
    col_step = r * math.sqrt(3)
    row_step = r * 1.5

    hex_cells = []
    hex_centres = []
    row = 0
    y = bounds[1] - r
    while y <= bounds[3] + r:
        x_offset = col_step / 2 if row % 2 else 0
        x = bounds[0] - r + x_offset
        while x <= bounds[2] + r:
            verts = [
                (x + r * math.cos(math.radians(60 * i)),
                 y + r * math.sin(math.radians(60 * i)))
                for i in range(6)
            ]
            hex_cells.append(Polygon(verts))
            hex_centres.append((x, y))
            x += col_step
        y += row_step
        row += 1
    print(f"   Generated {len(hex_cells):,} hex cells")

    # ── 6. Intersect hex cells with IRIS → area-weighted population ──
    print("   Computing hex ∩ IRIS intersections ...")
    iris_geom_arr = iris_gdf.geometry.values
    iris_pop_arr = iris_gdf["population"].values
    iris_area_arr = iris_gdf["iris_area"].values
    tree = STRtree(iris_geom_arr)

    results = []
    n_hex = len(hex_cells)
    for idx in range(n_hex):
        if idx % 10000 == 0 and idx > 0:
            print(f"\r   {idx:,}/{n_hex:,} hex cells ...", end="", flush=True)

        hex_poly = hex_cells[idx]
        candidates = tree.query(hex_poly)
        if len(candidates) == 0:
            continue

        total_pop = 0.0
        for ci in candidates:
            a = iris_area_arr[ci]
            if a <= 0:
                continue
            try:
                inter = hex_poly.intersection(iris_geom_arr[ci])
                if inter.is_empty:
                    continue
                total_pop += iris_pop_arr[ci] * (inter.area / a)
            except Exception:
                skipped_geom += 1
                continue

        if total_pop > 0.5:
            results.append((*hex_centres[idx], round(total_pop)))

    print(f"\r   {len(results):,} hex cells with population")
    if skipped_geom:
        print(f"   !! Skipped {skipped_geom} hex-IRIS intersection errors")

    # ── 7. Convert hex centres back to WGS84 for deck.gl ────────────
    from pyproj import Transformer
    transformer = Transformer.from_crs("EPSG:2154", "EPSG:4326", always_xy=True)
    xs, ys, pops = zip(*results) if results else ([], [], [])
    lons, lats = transformer.transform(list(xs), list(ys))

    total_pop = sum(pops)
    print(f"   Total population: {total_pop:,}")

    result = pd.DataFrame({
        "longitude": lons,
        "latitude": lats,
        "population": pops,
    })

    out = PROCESSED_DIR / "iris_population.parquet"
    result.to_parquet(out, index=False)
    print(f"   -> {len(result):,} points saved to {out.name}")
    return result


# ──────────────────────────────────────────────────────────────────────
# 2. Départements GeoJSON — boundaries for choropleth maps
# ──────────────────────────────────────────────────────────────────────

DEPARTEMENTS_GEOJSON_URL = (
    "https://raw.githubusercontent.com/gregoiredavid/france-geojson/"
    "master/departements-version-simplifiee.geojson"
)


def download_departements_geojson() -> dict:
    """Download simplified département boundaries GeoJSON."""
    print("[3/6] Downloading départements GeoJSON ...")
    resp = requests.get(DEPARTEMENTS_GEOJSON_URL, timeout=120)
    resp.raise_for_status()
    geojson = resp.json()

    out = PROCESSED_DIR / "departements.geojson"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(geojson, f, ensure_ascii=False)
    print(f"   -> {len(geojson['features'])} départements saved to {out.name}")
    return geojson


# ──────────────────────────────────────────────────────────────────────
# 3. FINESS — healthcare establishments (hospitals, clinics, etc.)
# ──────────────────────────────────────────────────────────────────────

FINESS_DATASET_API = (
    "https://www.data.gouv.fr/api/1/datasets/"
    "finess-extraction-du-fichier-des-etablissements/"
)

# Status code -> classification
FINESS_STATUS_MAP = {
    "1": "Public",    # Etablissement public de santé
    "2": "Privé",     # PSPH par intégration
    "3": "Privé",     # PSPH par concession
    "4": "Privé",     # Etablissement privé à but lucratif
    "6": "Privé",     # Etablissement de santé privé d'intérêt collectif
    "7": "Privé",     # Privé non lucratif, non déclaré d'intérêt collectif
}


def download_finess() -> pd.DataFrame:
    """
    Download the FINESS geocoded extract (establishments + coordinates).

    The file contains two record types:
      - 'structureet' (32 fields): establishment info
      - 'geolocalisation' (6 fields): Lambert-93 coordinates per FINESS number
    """
    print("[4/6] Downloading FINESS (établissements de santé) ...")

    # Discover the geocoded resource URL
    resp = requests.get(FINESS_DATASET_API, timeout=30)
    resp.raise_for_status()
    resources = resp.json().get("resources", [])

    geo_resource = None
    for r in resources:
        title = (r.get("title") or "").lower()
        if "localis" in title and r.get("format", "").lower() == "csv":
            geo_resource = r
            break

    if geo_resource is None:
        print("   !! Could not find the geocoded FINESS resource")
        return pd.DataFrame()

    url = geo_resource["url"]
    size_mb = (geo_resource.get("filesize") or 0) / 1024 / 1024
    print(f"   Resource: {geo_resource['title']} ({size_mb:.0f} MB)")

    # Download the full file
    print("   Downloading ...")
    dl_resp = requests.get(url, timeout=300)
    dl_resp.raise_for_status()

    text = dl_resp.text

    # Save raw
    raw_path = RAW_DIR / "finess-geolocalise.csv"
    with open(raw_path, "w", encoding="utf-8") as f:
        f.write(text)

    lines = text.split("\n")
    print(f"   Total lines: {len(lines):,}")

    # Parse structureet records (establishment data)
    etab_records = []
    geo_records = []

    for line in lines[1:]:  # skip metadata header
        if not line.strip():
            continue
        fields = line.split(";")
        if fields[0] == "structureet" and len(fields) >= 32:
            etab_records.append({
                "finess": fields[1],
                "finess_juridique": fields[2],
                "nom_court": fields[3],
                "nom_complet": fields[4],
                "code_commune": fields[12],
                "code_departement": fields[13],
                "departement": fields[14],
                "code_postal_commune": fields[15],
                "categorie_code": fields[18],
                "categorie": fields[19],
                "categorie_agregee_code": fields[20],
                "categorie_agregee": fields[21],
                "statut_code": fields[26],
                "statut": fields[27],
            })
        elif fields[0] == "geolocalisation" and len(fields) >= 4:
            geo_records.append({
                "finess": fields[1],
                "x_lambert93": fields[2],
                "y_lambert93": fields[3],
            })

    print(f"   Establishments: {len(etab_records):,}")
    print(f"   Geolocation records: {len(geo_records):,}")
    if len(etab_records) < 5000:
        print(f"   !! WARNING: Only {len(etab_records):,} establishments parsed (expected ~45k+)")

    etab_df = pd.DataFrame(etab_records)
    geo_df = pd.DataFrame(geo_records)

    # Fix double-UTF-8 encoding on text columns.
    # The file mixes proper UTF-8 and double-encoded UTF-8, so we fix per-cell.
    def _fix_double_utf8(val):
        if not isinstance(val, str):
            return val
        try:
            decoded = val.encode("latin-1").decode("utf-8")
            return decoded if decoded != val else val
        except (UnicodeDecodeError, UnicodeEncodeError):
            return val

    text_cols = ["nom_court", "nom_complet", "departement", "categorie",
                 "categorie_agregee", "statut", "code_postal_commune"]
    for col in text_cols:
        if col in etab_df.columns:
            etab_df[col] = etab_df[col].apply(_fix_double_utf8)

    # Convert Lambert-93 (EPSG:2154) to WGS84 (EPSG:4326)
    from pyproj import Transformer

    transformer = Transformer.from_crs("EPSG:2154", "EPSG:4326", always_xy=True)

    geo_df["x_lambert93"] = pd.to_numeric(geo_df["x_lambert93"], errors="coerce")
    geo_df["y_lambert93"] = pd.to_numeric(geo_df["y_lambert93"], errors="coerce")
    geo_df = geo_df.dropna(subset=["x_lambert93", "y_lambert93"])

    lons, lats = transformer.transform(
        geo_df["x_lambert93"].values, geo_df["y_lambert93"].values
    )
    geo_df["longitude"] = lons
    geo_df["latitude"] = lats
    geo_df = geo_df.drop(columns=["x_lambert93", "y_lambert93"])

    # Merge establishment info with coordinates
    merged = etab_df.merge(geo_df, on="finess", how="left")

    # Add public/private classification
    merged["classification"] = merged["statut_code"].map(FINESS_STATUS_MAP).fillna("Autre")

    matched = merged["latitude"].notna().sum()
    total = max(len(merged), 1)
    print(f"   Geocoded: {matched:,}/{len(merged):,} ({matched / total * 100:.1f}%)")

    # Filter to mainland + DOM-TOM reasonable bounds
    merged["latitude"] = pd.to_numeric(merged["latitude"], errors="coerce")
    merged["longitude"] = pd.to_numeric(merged["longitude"], errors="coerce")

    out = PROCESSED_DIR / "etablissements_sante.parquet"
    merged.to_parquet(out, index=False)
    print(f"   -> Saved to {out.name}")
    return merged


# ──────────────────────────────────────────────────────────────────────
# 4. Annuaire Santé (RPPS) — individual healthcare professionals
# ──────────────────────────────────────────────────────────────────────

RPPS_DATASET_SLUG = (
    "annuaire-sante-extractions-des-donnees-en-libre-acces-"
    "des-professionnels-intervenant-dans-le-systeme-de-sante-rpps"
)
RPPS_DATASET_API = f"https://www.data.gouv.fr/api/1/datasets/{RPPS_DATASET_SLUG}/"

# The main file we need: personne-activite (person + practice location)
RPPS_MAIN_FILE_KEYWORD = "personne-activite"

# Columns to keep from the pipe-delimited RPPS file.
# Keys = original column names (as they appear in the file),
# Values = our standardised short names.
KEEP_COLS_MAP = {
    "Identification nationale PP": "identifiant_pp",
    "Nom d'exercice": "nom",
    "Prénom d'exercice": "prenom",
    "Code profession": "code_profession",
    "Libellé profession": "profession",
    "Code savoir-faire": "code_savoir_faire",
    "Libellé savoir-faire": "savoir_faire",
    "Code mode exercice": "code_mode_exercice",
    "Libellé mode exercice": "mode_exercice",
    "Code postal (coord. structure)": "code_postal",
    "Code commune (coord. structure)": "code_commune",
    "Libellé commune (coord. structure)": "commune",
    "Code Département (structure)": "code_departement",
    "Libellé Département (structure)": "departement",
    "Code secteur d'activité": "code_secteur",
    "Libellé secteur d'activité": "secteur_activite",
}


def download_annuaire_sante() -> pd.DataFrame:
    """
    Download the RPPS open-access extract from data.gouv.fr.

    We only download the main file (ps-libreacces-personne-activite.txt, ~765 MB).
    It is pipe-delimited with 56 columns.
    """
    print("[5/6] Downloading Annuaire Santé (RPPS) from data.gouv.fr ...")

    # Step 1: discover the latest resource URL via the API
    resp = requests.get(RPPS_DATASET_API, timeout=30)
    resp.raise_for_status()
    dataset = resp.json()

    target = None
    for r in dataset.get("resources", []):
        title = (r.get("title") or "").lower()
        url = (r.get("url") or "").lower()
        if RPPS_MAIN_FILE_KEYWORD in title or RPPS_MAIN_FILE_KEYWORD in url:
            target = r
            break

    if target is None:
        print("   !! Could not find the personne-activite resource.")
        print(f"   !! Check: https://www.data.gouv.fr/fr/datasets/{RPPS_DATASET_SLUG}/")
        return pd.DataFrame()

    url = target["url"]
    size_mb = (target.get("filesize") or 0) / 1024 / 1024
    print(f"   Resource: {target['title']} ({size_mb:.0f} MB)")
    print(f"   Downloading (this may take a few minutes) ...")

    # Step 2: stream-download the file
    raw_path = RAW_DIR / "ps-libreacces-personne-activite.txt"
    dl_resp = requests.get(url, timeout=600, stream=True)
    dl_resp.raise_for_status()

    downloaded = 0
    with open(raw_path, "wb") as f:
        for chunk in dl_resp.iter_content(chunk_size=1 << 20):  # 1 MB chunks
            f.write(chunk)
            downloaded += len(chunk)
            mb = downloaded / 1024 / 1024
            print(f"\r   Downloaded {mb:.0f} MB ...", end="", flush=True)

    print(f"\r   Downloaded {downloaded / 1024 / 1024:.0f} MB -> {raw_path.name}")

    # Step 3: read with pandas (pipe-delimited, UTF-8 with BOM)
    print("   Parsing CSV ...")
    df = pd.read_csv(
        raw_path,
        sep="|",
        encoding="utf-8-sig",
        dtype=str,
        low_memory=False,
        on_bad_lines="skip",
    )
    print(f"   Raw rows: {len(df):,}, columns: {len(df.columns)}")
    if len(df) < 500000:
        print(f"   !! WARNING: Only {len(df):,} rows parsed (expected ~1M+)")

    # Step 4: rename & keep only the columns we need
    df = _standardize_columns(df)

    # Step 5: drop rows without a commune (professionals with no active practice)
    if "code_commune" in df.columns:
        before = len(df)
        df = df[df["code_commune"].notna() & (df["code_commune"].str.strip() != "")]
        print(f"   Kept {len(df):,} rows with a practice location (dropped {before - len(df):,})")

    # Step 6: deduplicate (same professional at same commune)
    if "identifiant_pp" in df.columns and "code_commune" in df.columns:
        before = len(df)
        df = df.drop_duplicates(subset=["identifiant_pp", "code_commune"], keep="first")
        print(f"   After dedup: {len(df):,} rows (removed {before - len(df):,})")

    out = PROCESSED_DIR / "professionnels_sante.parquet"
    df.to_parquet(out, index=False)
    print(f"   -> Saved to {out.name}")
    return df


def _standardize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Rename known columns to our standard names, drop unknowns."""
    # Strip BOM and whitespace from column names
    df.columns = df.columns.str.strip().str.strip("\ufeff")

    rename_map = {}
    for orig, std in KEEP_COLS_MAP.items():
        if orig in df.columns:
            rename_map[orig] = std
        else:
            # Fallback: case-insensitive + stripped comparison
            for col in df.columns:
                if col.strip().lower() == orig.strip().lower():
                    rename_map[col] = std
                    break

    if not rename_map:
        print("   !! WARNING: No known columns found. Keeping all columns as-is.")
        print(f"   !! Available columns: {list(df.columns[:10])} ...")
        return df

    print(f"   Matched {len(rename_map)}/{len(KEEP_COLS_MAP)} expected columns")
    df = df.rename(columns=rename_map)
    known_cols = [c for c in rename_map.values() if c in df.columns]
    return df[known_cols].copy()


# ──────────────────────────────────────────────────────────────────────
# 5. Post-processing: enrich professionals with commune coordinates
# ──────────────────────────────────────────────────────────────────────


# Paris, Lyon, and Marseille use arrondissement codes in RPPS.
# If the communes data has arrondissements directly, no mapping is needed.
# Otherwise, fall back to mapping to the parent commune code.
ARRONDISSEMENT_TO_PARENT = {}
# Paris: 75101-75120 -> 75056
for _i in range(1, 21):
    ARRONDISSEMENT_TO_PARENT[f"751{_i:02d}"] = "75056"
# Lyon: 69381-69389 -> 69123
for _i in range(1, 10):
    ARRONDISSEMENT_TO_PARENT[f"6938{_i}"] = "69123"
# Marseille: 13201-13216 -> 13055
for _i in range(1, 17):
    ARRONDISSEMENT_TO_PARENT[f"132{_i:02d}"] = "13055"


def enrich_with_coordinates(
    prof_df: pd.DataFrame, communes_df: pd.DataFrame
) -> pd.DataFrame:
    """
    Join professionals with commune data to get:
      - lat/lon coordinates
      - department name/code (the RPPS file leaves these empty)
      - region code
      - commune population
    """
    if prof_df.empty or communes_df.empty:
        return prof_df

    print("[6/6] Enriching professionals with commune data ...")

    prof_df = prof_df.copy()
    prof_df["code_commune"] = prof_df["code_commune"].astype(str).str.strip()
    communes_df = communes_df.copy()
    communes_df["code_commune"] = communes_df["code_commune"].astype(str).str.strip()

    # Map arrondissement codes to parent commune ONLY if the arrondissement
    # code doesn't exist directly in the communes data (i.e. communes data
    # only has the parent code, not the individual arrondissements).
    commune_codes_available = set(communes_df["code_commune"])
    mapping = {
        arr: parent
        for arr, parent in ARRONDISSEMENT_TO_PARENT.items()
        if arr not in commune_codes_available and parent in commune_codes_available
    }
    prof_df["code_commune_join"] = prof_df["code_commune"].map(mapping).fillna(
        prof_df["code_commune"]
    )
    arrond_count = (prof_df["code_commune"] != prof_df["code_commune_join"]).sum()
    if arrond_count:
        print(f"   Mapped {arrond_count:,} arrondissement codes to parent communes")

    # Drop the empty department columns from RPPS before merging
    for col in ["code_departement", "departement"]:
        if col in prof_df.columns:
            prof_df = prof_df.drop(columns=[col])

    enrich_cols = communes_df[
        ["code_commune", "nom_commune", "code_departement", "code_region",
         "population", "latitude", "longitude"]
    ].rename(columns={
        "population": "population_commune",
        "code_commune": "code_commune_join",
    })

    merged = prof_df.merge(enrich_cols, on="code_commune_join", how="left")
    merged = merged.drop(columns=["code_commune_join"])

    # Build a human-readable department name from the GeoJSON if available
    geojson_path = PROCESSED_DIR / "departements.geojson"
    if geojson_path.exists():
        with open(geojson_path, "r", encoding="utf-8") as f:
            geojson = json.load(f)
        dept_names = {
            feat["properties"]["code"]: feat["properties"].get("nom", "")
            for feat in geojson["features"]
        }
        merged["departement"] = merged["code_departement"].map(dept_names)
    else:
        merged["departement"] = merged["code_departement"]

    matched = merged["latitude"].notna().sum()
    total = len(merged)
    print(f"   -> {matched:,}/{total:,} professionals matched with coordinates ({matched / total * 100:.1f}%)")

    out = PROCESSED_DIR / "professionnels_sante.parquet"
    merged.to_parquet(out, index=False)
    print(f"   -> Updated {out.name}")
    return merged


# ──────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────


def main():
    print("=" * 60)
    print("Healthcare France Mapping — Data Download")
    print("=" * 60)
    print()

    communes_df = download_communes()
    print()

    download_iris(communes_df)
    print()

    download_departements_geojson()
    print()

    download_finess()
    print()

    prof_df = download_annuaire_sante()
    print()

    if not prof_df.empty and not communes_df.empty:
        enrich_with_coordinates(prof_df, communes_df)

    print()
    print("=" * 60)
    print("Done! Data saved in:", PROCESSED_DIR)
    print("You can now run:  streamlit run app/🏠_Accueil.py")
    print("=" * 60)


if __name__ == "__main__":
    main()
