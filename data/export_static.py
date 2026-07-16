#!/usr/bin/env python3
"""
Export processed parquet data to compact JSON files for the static GitHub Pages site.

Usage:
    python data/export_static.py

Reads from:  data/processed/*.parquet + departements.geojson
Writes to:   docs/data/*.json
"""

import json
import shutil
from pathlib import Path

import pandas as pd

PROCESSED_DIR = Path(__file__).resolve().parent / "processed"
DOCS_DATA_DIR = Path(__file__).resolve().parent.parent / "docs" / "data"

DOCS_DATA_DIR.mkdir(parents=True, exist_ok=True)


def export_communes_professions():
    """
    Export commune-level professional counts by profession.

    Output structure:
    {
      "communes": {"75101": {"n":"Paris 1er","d":"75","dn":"Paris","la":48.86,"lo":2.34}, ...},
      "counts":   {"75101": {"Médecin":150,"Infirmier":80}, ...},
      "professions": ["Médecin", "Infirmier", ...]
    }
    """
    print("[1/5] Exporting communes + professions ...")

    prof_df = pd.read_parquet(PROCESSED_DIR / "professionnels_sante.parquet")
    communes_df = pd.read_parquet(PROCESSED_DIR / "communes.parquet")

    # Only keep professionals with coordinates
    prof_df = prof_df.dropna(subset=["latitude", "longitude"])

    # Unique professionals per commune × profession
    counts = (
        prof_df.groupby(["code_commune", "profession"])["identifiant_pp"]
        .nunique()
        .reset_index(name="count")
    )

    # Pivot to {code_commune: {profession: count}}
    counts_dict = {}
    for _, row in counts.iterrows():
        cc = row["code_commune"]
        if cc not in counts_dict:
            counts_dict[cc] = {}
        counts_dict[cc][row["profession"]] = int(row["count"])

    # Communes metadata — only communes that have at least one professional
    active_communes = set(counts_dict.keys())
    communes_df = communes_df[communes_df["code_commune"].isin(active_communes)].copy()

    # Build department name lookup from geojson if available
    dept_names = {}
    geojson_path = PROCESSED_DIR / "departements.geojson"
    if geojson_path.exists():
        with open(geojson_path, encoding="utf-8") as f:
            gj = json.load(f)
        dept_names = {
            feat["properties"]["code"]: feat["properties"].get("nom", "")
            for feat in gj["features"]
        }

    communes_out = {}
    for _, row in communes_df.iterrows():
        cc = row["code_commune"]
        communes_out[cc] = {
            "n": row.get("nom_commune", ""),
            "d": row.get("code_departement", ""),
            "dn": dept_names.get(row.get("code_departement", ""), row.get("code_departement", "")),
            "la": round(float(row["latitude"]), 4),
            "lo": round(float(row["longitude"]), 4),
        }

    professions = sorted(prof_df["profession"].dropna().unique().tolist())

    result = {
        "communes": communes_out,
        "counts": counts_dict,
        "professions": professions,
    }

    out = DOCS_DATA_DIR / "communes_professions.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, separators=(",", ":"))
    size_mb = out.stat().st_size / 1024 / 1024
    print(f"   -> {out.name} ({size_mb:.1f} MB, {len(communes_out):,} communes, {len(professions)} professions)")


def export_dept_stats():
    """
    Export department-level stats for density/desert pages.

    Output: {"75": {"name":"Paris","pop":2161000,"pros":{"Médecin":12500,...}}, ...}
    """
    print("[2/5] Exporting department stats ...")

    prof_df = pd.read_parquet(PROCESSED_DIR / "professionnels_sante.parquet")
    communes_df = pd.read_parquet(PROCESSED_DIR / "communes.parquet")

    # Department populations
    dept_pop = (
        communes_df.groupby("code_departement")["population"]
        .sum()
        .to_dict()
    )

    # Professional counts by department × profession (unique identifiant_pp)
    prof_df = prof_df.dropna(subset=["code_departement"])
    dept_prof = (
        prof_df.groupby(["code_departement", "profession"])["identifiant_pp"]
        .nunique()
        .reset_index(name="count")
    )

    # Department names from geojson
    dept_names = {}
    geojson_path = PROCESSED_DIR / "departements.geojson"
    if geojson_path.exists():
        with open(geojson_path, encoding="utf-8") as f:
            gj = json.load(f)
        dept_names = {
            feat["properties"]["code"]: feat["properties"].get("nom", "")
            for feat in gj["features"]
        }

    result = {}
    all_depts = set(dept_pop.keys()) | set(dept_prof["code_departement"].unique())
    for dept in sorted(all_depts):
        pros = {}
        dept_rows = dept_prof[dept_prof["code_departement"] == dept]
        for _, row in dept_rows.iterrows():
            pros[row["profession"]] = int(row["count"])
        result[dept] = {
            "name": dept_names.get(dept, dept),
            "pop": int(dept_pop.get(dept, 0)),
            "pros": pros,
        }

    out = DOCS_DATA_DIR / "dept_stats.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, separators=(",", ":"))
    size_kb = out.stat().st_size / 1024
    print(f"   -> {out.name} ({size_kb:.0f} KB, {len(result)} departments)")


def export_departements_geojson():
    """Copy the departments GeoJSON as-is."""
    print("[3/5] Copying departements GeoJSON ...")
    src = PROCESSED_DIR / "departements.geojson"
    dst = DOCS_DATA_DIR / "departements.geojson"
    shutil.copy2(src, dst)
    size_kb = dst.stat().st_size / 1024
    print(f"   -> {dst.name} ({size_kb:.0f} KB)")


def export_hex_population():
    """
    Export hex grid population as compact array-of-arrays.

    Output: [[lon, lat, pop], [lon, lat, pop], ...]
    """
    print("[4/5] Exporting hex population ...")

    df = pd.read_parquet(PROCESSED_DIR / "iris_population.parquet")
    df = df.dropna(subset=["latitude", "longitude"])
    df = df[df["population"] > 0]

    data = []
    for _, row in df.iterrows():
        data.append([
            round(float(row["longitude"]), 4),
            round(float(row["latitude"]), 4),
            int(row["population"]),
        ])

    out = DOCS_DATA_DIR / "hex_population.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"))
    size_kb = out.stat().st_size / 1024
    print(f"   -> {out.name} ({size_kb:.0f} KB, {len(data):,} hex cells)")


def export_etablissements():
    """
    Export healthcare establishments (Public/Privé only, geocoded).

    Output: [{"n":"CHU ...","la":48.84,"lo":2.36,"c":"Public","t":"...","ta":"...","d":"...","cp":"..."}, ...]
    """
    print("[5/5] Exporting etablissements ...")

    df = pd.read_parquet(PROCESSED_DIR / "etablissements_sante.parquet")

    # Keep only geocoded Public/Privé
    df["latitude"] = pd.to_numeric(df["latitude"], errors="coerce")
    df["longitude"] = pd.to_numeric(df["longitude"], errors="coerce")
    df = df.dropna(subset=["latitude", "longitude"])
    df = df[df["classification"].isin(["Public", "Privé"])]

    data = []
    for _, row in df.iterrows():
        data.append({
            "n": row.get("nom_complet") or row.get("nom_court") or "",
            "la": round(float(row["latitude"]), 4),
            "lo": round(float(row["longitude"]), 4),
            "c": row.get("classification", ""),
            "t": row.get("categorie", ""),
            "ta": row.get("categorie_agregee", ""),
            "d": row.get("departement", ""),
            "cp": row.get("code_postal_commune", ""),
        })

    out = DOCS_DATA_DIR / "etablissements.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    size_mb = out.stat().st_size / 1024 / 1024
    print(f"   -> {out.name} ({size_mb:.1f} MB, {len(data):,} establishments)")


def main():
    print("=" * 60)
    print("Healthcare France Mapping — Export Static Data")
    print("=" * 60)
    print()

    export_communes_professions()
    print()
    export_dept_stats()
    print()
    export_departements_geojson()
    print()
    export_hex_population()
    print()
    export_etablissements()

    print()
    total_size = sum(f.stat().st_size for f in DOCS_DATA_DIR.iterdir()) / 1024 / 1024
    print(f"Total: {total_size:.1f} MB in {DOCS_DATA_DIR}")
    print("=" * 60)


if __name__ == "__main__":
    main()
