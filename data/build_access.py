#!/usr/bin/env python3
"""
Build the "where should the next practitioner set up?" analysis layer.

Reworks the descriptive atlas into a decision tool. It answers one question,
for any of 14 health professions:

    Where is access to a [doctor / physio / dentist / ...] the worst,
    and where would a new practitioner serve the most under-served people?

The binding constraint in France is not raw distance to *a* practitioner
(almost everyone is near one) but local DENSITY per capita -- the "medical
desert" / accessibility problem. So for every populated location we measure,
within a local travel basin (a straight-line catchment used as an honest,
reproducible proxy for a ~15 min drive), the number of practitioners per 100k
residents, and compare it to the national average.

Two rankings of candidate sites, in plain language:
  - "Le plus d'habitants concernés"  -> basins where the most under-served
     residents live (tends to surface peri-urban rings).
  - "Accès le plus difficile"        -> basins with the fewest practitioners
     per resident (tends to surface rural areas).

Note: counts come from the public RPPS export at the *profession* level
(e.g. "Médecin" lumps GPs + specialists). The local-vs-national comparison is
internally consistent because both sides use the same definition.

Inputs  (already committed, no download needed):
    docs/data/communes_professions.json   communes (coords) + counts per profession
    docs/data/hex_population.json          [[lon, lat, pop], ...] population grid

Outputs (consumed by the static site):
    docs/data/access_layers.json           per-point local density, per profession
    docs/data/priority_basins.json         ranked candidate sites, per profession
    docs/data/opportunity.json             headline KPIs, per profession

Run:
    uv run --python 3.12 --with numpy python data/build_access.py
"""

import json
from pathlib import Path

import numpy as np

DATA_DIR = Path(__file__).resolve().parent.parent / "docs" / "data"

# --- Analysis parameters (defensible, documented) ---------------------------
CATCHMENT_RADIUS_KM = 15.0   # local travel basin (proxy for ~15 min drive)
UNDERSERVED_RATIO = 0.70     # under-served = local density < 70% of national average
TOP_N_BASINS = 15
MIN_CATCH_POP = 3000         # ignore near-empty catchments (noise)
EARTH_RADIUS_KM = 6371.0

# Curated patient-facing liberal professions (order = UI order, first = default).
# Excludes the administrative "Acteur du système de santé..." role and
# professions too sparse (<5k) for a reliable density analysis.
PROFESSIONS = [
    "Médecin",
    "Chirurgien-Dentiste",
    "Masseur-Kinésithérapeute",
    "Infirmier",
    "Sage-Femme",
    "Orthophoniste",
    "Pharmacien",
    "Pédicure-Podologue",
    "Psychologue",
    "Ostéopathe",
    "Opticien-Lunetier",
    "Ergothérapeute",
    "Orthoptiste",
    "Diététicien",
]


def haversine_matrix(lat_a, lon_a, lat_b, lon_b):
    """Pairwise haversine (km) between points A (len n) and B (len m) -> (n, m)."""
    lat_a = np.radians(lat_a)[:, None]
    lon_a = np.radians(lon_a)[:, None]
    lat_b = np.radians(lat_b)[None, :]
    lon_b = np.radians(lon_b)[None, :]
    dlat = lat_b - lat_a
    dlon = lon_b - lon_a
    h = np.sin(dlat / 2) ** 2 + np.cos(lat_a) * np.cos(lat_b) * np.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * np.arcsin(np.sqrt(h))


def load_inputs():
    with open(DATA_DIR / "communes_professions.json", encoding="utf-8") as f:
        cp = json.load(f)
    with open(DATA_DIR / "hex_population.json", encoding="utf-8") as f:
        pop = json.load(f)

    communes = cp["communes"]
    counts = cp["counts"]
    codes = list(communes.keys())

    c_lat = np.array([communes[c]["la"] for c in codes], dtype=float)
    c_lon = np.array([communes[c]["lo"] for c in codes], dtype=float)
    # counts matrix: communes x professions
    counts_mat = np.array(
        [[counts.get(c, {}).get(p, 0) for p in PROFESSIONS] for c in codes],
        dtype=float,
    )

    pop = np.array(pop, dtype=float)  # [lon, lat, pop]
    p_lon, p_lat, p_pop = pop[:, 0], pop[:, 1], pop[:, 2]
    return communes, codes, c_lat, c_lon, counts_mat, p_lat, p_lon, p_pop


def point_density(p_lat, p_lon, c_lat, c_lon, counts_mat, p_pop, chunk=300):
    """Local practitioner density (per 100k) at each point, for every profession."""
    n = len(p_lat)
    docs = np.zeros((n, len(PROFESSIONS)))
    pop_local = np.zeros(n)
    for i in range(0, n, chunk):
        j = min(i + chunk, n)
        mask_c = haversine_matrix(p_lat[i:j], p_lon[i:j], c_lat, c_lon) <= CATCHMENT_RADIUS_KM
        docs[i:j] = mask_c @ counts_mat
        mask_p = haversine_matrix(p_lat[i:j], p_lon[i:j], p_lat, p_lon) <= CATCHMENT_RADIUS_KM
        pop_local[i:j] = mask_p @ p_pop
    pop_local = np.where(pop_local > 0, pop_local, np.nan)
    return docs / pop_local[:, None] * 1e5  # (points, prof)


def candidate_metrics(c_lat, c_lon, counts_mat, p_lat, p_lon, p_pop,
                      underserved_pop_mat, chunk=200):
    """For each candidate commune: catchment population, local density, and the
    under-served population it covers -- per profession."""
    n = len(c_lat)
    catch_pop = np.zeros(n)
    catch_docs = np.zeros((n, len(PROFESSIONS)))
    catch_under = np.zeros((n, len(PROFESSIONS)))
    for i in range(0, n, chunk):
        j = min(i + chunk, n)
        mask_p = haversine_matrix(c_lat[i:j], c_lon[i:j], p_lat, p_lon) <= CATCHMENT_RADIUS_KM
        catch_pop[i:j] = mask_p @ p_pop
        catch_under[i:j] = mask_p @ underserved_pop_mat
        mask_c = haversine_matrix(c_lat[i:j], c_lon[i:j], c_lat, c_lon) <= CATCHMENT_RADIUS_KM
        catch_docs[i:j] = mask_c @ counts_mat
    cp = np.where(catch_pop > 0, catch_pop, np.nan)
    density = catch_docs / cp[:, None] * 1e5
    return catch_pop, density, catch_under, catch_docs


def dedupe_rank(order, c_lat, c_lon, communes, codes, density_col, catch_pop,
                catch_under_col, docs_col, target, limit=TOP_N_BASINS):
    """Walk a pre-sorted order, keep geographically distinct basins (non-overlap)."""
    out, seen = [], []
    for idx in order:
        if catch_pop[idx] < MIN_CATCH_POP:
            continue
        lat, lon = c_lat[idx], c_lon[idx]
        if any(haversine_matrix(np.array([lat]), np.array([lon]),
                                np.array([s[0]]), np.array([s[1]]))[0, 0]
               < CATCHMENT_RADIUS_KM for s in seen):
            continue
        seen.append((lat, lon))
        c = codes[idx]
        out.append({
            "name": communes[c]["n"],
            "dept": communes[c]["d"],
            "dn": communes[c]["dn"],
            "lat": round(lat, 4),
            "lon": round(lon, 4),
            "catchment_pop": int(catch_pop[idx]),
            "density": None if np.isnan(density_col[idx]) else round(float(density_col[idx]), 1),
            "underserved_pop": int(catch_under_col[idx]),
            # practitioners to add to bring the basin back to the 70% threshold
            "missing": int(max(0.0, np.ceil(target * catch_pop[idx] / 1e5 - docs_col[idx]))),
        })
        if len(out) >= limit:
            break
    return out


def main():
    print("Loading inputs ...")
    communes, codes, c_lat, c_lon, counts_mat, p_lat, p_lon, p_pop = load_inputs()
    total_pop = float(p_pop.sum())
    nat_density = counts_mat.sum(axis=0) / total_pop * 1e5  # per profession
    print(f"  {len(codes):,} communes | {len(p_lat):,} points | {len(PROFESSIONS)} professions")

    print("Computing local density per point ...")
    p_density = point_density(p_lat, p_lon, c_lat, c_lon, counts_mat, p_pop)
    target = UNDERSERVED_RATIO * nat_density  # (prof,)
    p_under = np.nan_to_num(p_density, nan=0.0) < target[None, :]  # (points, prof) bool
    underserved_pop_mat = p_pop[:, None] * p_under  # (points, prof)

    print("Computing candidate basins ...")
    catch_pop, c_density, c_under, c_docs = candidate_metrics(
        c_lat, c_lon, counts_mat, p_lat, p_lon, p_pop, underserved_pop_mat)

    # --- Build outputs ---
    basins = {}
    opportunity = {"total_pop": int(total_pop), "underserved_ratio": UNDERSERVED_RATIO,
                   "radius_km": CATCHMENT_RADIUS_KM, "professions": {}}
    density_layers = {}

    for k, prof in enumerate(PROFESSIONS):
        # rankings
        by_pop_order = np.argsort(-c_under[:, k])                       # most under-served residents
        scarce = np.where(c_density[:, k] < target[k], c_density[:, k], np.inf)
        by_scarce_order = np.lexsort((-c_under[:, k], scarce))          # lowest density, ties -> more people

        basins[prof] = {
            "by_population": dedupe_rank(by_pop_order, c_lat, c_lon, communes, codes,
                                         c_density[:, k], catch_pop, c_under[:, k],
                                         c_docs[:, k], target[k]),
            "by_scarcity": dedupe_rank(by_scarce_order, c_lat, c_lon, communes, codes,
                                       c_density[:, k], catch_pop, c_under[:, k],
                                       c_docs[:, k], target[k]),
        }

        under_pop = float(p_pop[p_under[:, k]].sum())
        opportunity["professions"][prof] = {
            "national_density": round(float(nat_density[k]), 1),
            "underserved_pop": int(under_pop),
            "underserved_pct": round(100 * under_pop / total_pop, 1),
            "top_underserved_pop": int(sum(b["underserved_pop"]
                                           for b in basins[prof]["by_population"])),
        }

        # per-point density (int, for the map) — 0 stored where no local pop
        density_layers[prof] = [int(round(d)) if not np.isnan(d) else 0
                                for d in p_density[:, k]]

    points = [[round(float(p_lon[i]), 4), round(float(p_lat[i]), 4), int(p_pop[i])]
              for i in range(len(p_lat))]

    with open(DATA_DIR / "access_layers.json", "w", encoding="utf-8") as f:
        json.dump({
            "professions": PROFESSIONS,
            "radius_km": CATCHMENT_RADIUS_KM,
            "underserved_ratio": UNDERSERVED_RATIO,
            "national_density": {p: round(float(nat_density[i]), 1)
                                 for i, p in enumerate(PROFESSIONS)},
            "points": points,
            "density": density_layers,
        }, f, ensure_ascii=False, separators=(",", ":"))

    with open(DATA_DIR / "priority_basins.json", "w", encoding="utf-8") as f:
        json.dump({"radius_km": CATCHMENT_RADIUS_KM,
                   "national_density": {p: round(float(nat_density[i]), 1)
                                        for i, p in enumerate(PROFESSIONS)},
                   "basins": basins}, f, ensure_ascii=False, separators=(",", ":"))

    with open(DATA_DIR / "opportunity.json", "w", encoding="utf-8") as f:
        json.dump(opportunity, f, ensure_ascii=False, separators=(",", ":"))

    # --- Report ---
    print(f"\nPopulation covered: {int(total_pop):,}\n")
    print(f"{'Profession':<26}{'Nat./100k':>10}{'Under-served':>14}{'%':>7}")
    for prof in PROFESSIONS:
        o = opportunity["professions"][prof]
        print(f"{prof[:25]:<26}{o['national_density']:>10.1f}"
              f"{o['underserved_pop']:>14,}{o['underserved_pct']:>6.1f}%")

    print("\n=== MEDECIN - 'Le plus d'habitants concernes' (top 8) ===")
    for i, b in enumerate(basins["Médecin"]["by_population"][:8], 1):
        print(f"{i:>2}  {b['name'][:24]:<25} {b['dn'][:18]:<19} "
              f"pop~{b['catchment_pop']:>9,}  sous-dotes~{b['underserved_pop']:>9,}")
    print("\n=== MEDECIN - 'Acces le plus difficile' (top 8) ===")
    for i, b in enumerate(basins["Médecin"]["by_scarcity"][:8], 1):
        d = b["density"] or 0
        print(f"{i:>2}  {b['name'][:24]:<25} {b['dn'][:18]:<19} "
              f"{d:>6.1f}/100k  pop~{b['catchment_pop']:>9,}")


if __name__ == "__main__":
    main()
