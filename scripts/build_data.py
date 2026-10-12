"""Tableaux AIFS ENS par département : moyenne, médiane, P10 et P90 des 51 membres, H+0 à H+360.

Même format « points + communes » que les autres ensembles (PEARP, GEFS, AIGEFS) : un fichier par
département, `forecast` = moyenne, `forecast_statistics` = médiane, p10, p90. Colonnes :
température à 2 m (°C), pluie des 6 dernières heures (mm, absente à H+0) et pluie cumulée depuis
le run (mm). Les statistiques sont calculées membre par membre puis sur les 51 membres.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import requests

from source import MEMBERS, STEPS, decode, grid_index, latest_run, retrieve

VERSION = "1.0.0"
REPO = "alertesmeteo-hub/AIFS-ENS-25-km"
COLONNES = ["temperature_c", "precipitation_mm", "precipitation_total_mm"]
STATISTIQUES = ("mean", "median", "p10", "p90")
CATALOGUE = Path(__file__).resolve().parent.parent / "config" / "communes-france.json"


def iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def charger_catalogue(path: Path = CATALOGUE):
    """Points de grille utiles (un par maille contenant au moins une commune) et communes par département."""
    communes = json.loads(path.read_text(encoding="utf-8"))["communes"]
    points: dict[int, tuple[float, float]] = {}
    par_dep: dict[str, list[tuple[list, int]]] = defaultdict(list)
    for c in communes:
        idx, lat, lon = grid_index(float(c[5]), float(c[6]))
        points[idx] = (lat, lon)
        par_dep[str(c[2])].append((c, idx))
    indexes = sorted(points)
    return indexes, points, par_dep, len(communes)


def statistiques(valeurs: np.ndarray) -> dict[str, np.ndarray]:
    """valeurs : (membres, …) -> moyenne, médiane, p10, p90 sur l'axe des membres."""
    if valeurs.shape[0] != MEMBERS:
        raise ValueError(f"{valeurs.shape[0]} membres au lieu de {MEMBERS}")
    if not np.isfinite(valeurs).all():
        raise ValueError("valeurs non finies")
    return {
        "mean": valeurs.mean(axis=0),
        "median": np.median(valeurs, axis=0),
        "p10": np.quantile(valeurs, 0.1, axis=0),
        "p90": np.quantile(valeurs, 0.9, axis=0),
    }


def pluie_6h(total: np.ndarray) -> np.ndarray:
    """Pluie de chaque pas de 6 h à partir du cumul depuis le run (membres, échéances, points) ; NaN à H+0."""
    pas = np.full(total.shape, np.nan)
    pas[:, 1:] = np.maximum(np.diff(total, axis=1), 0.0)
    return pas


def lignes(stats: dict[str, dict[str, np.ndarray]], stat: str, k: int, ids: list[int]) -> list[list]:
    """Lignes d'une échéance pour les points d'un département, dans l'ordre de COLONNES."""
    out = []
    for i in ids:
        pluie = stats["pluie"][stat][k, i]
        out.append([
            round(float(stats["temperature"][stat][k, i]), 2),
            None if not np.isfinite(pluie) else round(float(pluie), 2),
            round(float(stats["cumul"][stat][k, i]), 2),
        ])
    return out


def ecrire(run: datetime, indexes, points, par_dep, n_communes: int, stats, sortie: Path) -> dict:
    position = {idx: i for i, idx in enumerate(indexes)}
    (sortie / "departements").mkdir(parents=True, exist_ok=True)
    colonnes = {
        "points": ["model_index", "latitude", "longitude", "altitude_m"],
        "communes": ["code_insee", "name", "postal_codes", "population", "latitude", "longitude", "point_id"],
        "values": COLONNES,
    }
    temps = [iso(run + timedelta(hours=h)) for h in STEPS]
    deps = {}
    for code, entrees in sorted(par_dep.items()):
        idx_dep = sorted({idx for _, idx in entrees})
        local = {idx: j for j, idx in enumerate(idx_dep)}
        ids = [position[idx] for idx in idx_dep]
        prev = {
            stat: [[temps[k], lignes(stats, stat, k, ids)] for k in range(len(STEPS))]
            for stat in STATISTIQUES
        }
        payload = {
            "schema_version": 4,
            "columns": colonnes,
            "department": code,
            "members": MEMBERS,
            "statistic": "mean",
            "run_time": iso(run),
            "points": [[idx, points[idx][0], points[idx][1], None] for idx in idx_dep],
            "communes": [[c[0], c[1], c[3], c[4], c[5], c[6], local[idx]] for c, idx in entrees],
            "forecast": prev["mean"],
            "forecast_statistics": {s: prev[s] for s in STATISTIQUES if s != "mean"},
        }
        (sortie / "departements" / f"{code}.json").write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False), encoding="utf-8"
        )
        deps[code] = {"communes": len(entrees), "points": len(idx_dep)}
    index = {
        "schema_version": 4,
        "status": "ok",
        "version": VERSION,
        "run": iso(run),
        "generated_at": iso(datetime.now(timezone.utc)),
        "model": {
            "name": "AIFS ENS (ECMWF, IA)",
            "provider": "ECMWF",
            "dataset": "ECMWF Open Data — AIFS ENS",
            "members": MEMBERS,
            "resolution_degrees": 0.25,
            "forecast_hours_requested": STEPS[-1],
            "run_time": iso(run),
            "source_url": "https://www.ecmwf.int/en/forecasts/datasets/open-data",
            "license": "Creative Commons Attribution 4.0 International (CC BY 4.0)",
        },
        "members": MEMBERS,
        "table_steps": STEPS,
        "columns": COLONNES,
        "statistics": list(STATISTIQUES),
        "commune_count": n_communes,
        "departments": deps,
        "limitations": "Température à 2 m toutes les 6 h (pas de Tmax/Tmin publiées), pluie sur 6 h et cumulée depuis le run. "
        "Pas de rafales ni de neige. Statistiques des 51 membres (contrôle + 50 perturbés), pas un scénario déterministe.",
    }
    (sortie / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    return index


def run_publie() -> str | None:
    """Run actuellement publié sur la branche data (API GitHub : pas le CDN raw, qui peut avoir 5 min de retard)."""
    entetes = {"Accept": "application/vnd.github.raw"}
    if os.environ.get("GITHUB_TOKEN"):
        entetes["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    try:
        r = requests.get(f"https://api.github.com/repos/{REPO}/contents/index.json?ref=data", headers=entetes, timeout=30)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json().get("run")
    except (requests.RequestException, ValueError) as e:
        print(f"Publication actuelle illisible ({e}) : on reconstruit.", flush=True)
        return None


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--run", help="AAAAMMJJHH (sinon le plus récent complet)")
    p.add_argument("--force", action="store_true", help="reconstruire même si ce run est déjà publié")
    p.add_argument("--out-dir", default="build/data")
    p.add_argument("--cache", default="build/cache")
    p.add_argument("--workers", type=int, default=4)
    a = p.parse_args()

    run = datetime.strptime(a.run, "%Y%m%d%H").replace(tzinfo=timezone.utc) if a.run else latest_run()
    print(f"Run AIFS ENS : {iso(run)}", flush=True)
    if not a.force and run_publie() == iso(run):
        print("Ce run est déjà publié : rien à faire.", flush=True)
        return 0

    indexes, points, par_dep, n_communes = charger_catalogue()
    print(f"{n_communes} communes, {len(par_dep)} départements, {len(indexes)} points de grille", flush=True)
    temperature = np.full((MEMBERS, len(STEPS), len(indexes)), np.nan)
    cumul = np.full((MEMBERS, len(STEPS), len(indexes)), np.nan)
    cache = Path(a.cache) / run.strftime("%Y%m%d%H")
    cache.mkdir(parents=True, exist_ok=True)

    # Téléchargements en parallèle ; décodage (eccodes) dans le fil principal, fichier supprimé ensuite.
    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        taches = {pool.submit(retrieve, run, h, cache / f"{h:03d}.grib2"): k for k, h in enumerate(STEPS)}
        for n, t in enumerate(as_completed(taches), 1):
            k = taches[t]
            chemin = t.result()
            for (param, membre), v in decode(chemin, run, STEPS[k], indexes).items():
                if param == "2t":
                    temperature[membre, k] = v - 273.15
                else:
                    cumul[membre, k] = np.maximum(v, 0.0)
            chemin.unlink()
            if n % 10 == 0 or n == len(STEPS):
                print(f"{n}/{len(STEPS)} échéances validées", flush=True)

    if np.abs(cumul[:, 0]).max() > 0.01:
        raise ValueError("pluie cumulée non nulle à H+0")
    # Le codage GRIB peut faire reculer un cumul de quelques millièmes de mm d'une échéance à l'autre.
    cumul = np.maximum.accumulate(cumul, axis=1)
    if not (-80 < np.nanmin(temperature) and np.nanmax(temperature) < 60):
        raise ValueError(f"températures hors bornes ({np.nanmin(temperature):.1f} / {np.nanmax(temperature):.1f} °C)")
    pluie = pluie_6h(cumul)
    stats = {
        "temperature": statistiques(temperature),
        "cumul": statistiques(cumul),
        "pluie": {s: np.full((len(STEPS), len(indexes)), np.nan) for s in STATISTIQUES},
    }
    for s, v in statistiques(pluie[:, 1:]).items():
        stats["pluie"][s][1:] = v
    index = ecrire(run, indexes, points, par_dep, n_communes, stats, Path(a.out_dir))
    print(f"Publication prête : run {index['run']}, {index['commune_count']} communes, {len(index['departments'])} départements", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
