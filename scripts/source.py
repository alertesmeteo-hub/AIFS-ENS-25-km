"""Lecture stricte de l'ensemble AIFS ENS d'ECMWF (données ouvertes, CC BY 4.0).

51 membres (contrôle 0 + perturbés 1 à 50), grille 0,25°, H+0 à H+360 par pas de 6 h,
4 runs par jour. On ne télécharge que la température à 2 m et la pluie cumulée depuis le run
(plages d'octets via les fichiers .index d'ECMWF, environ 77 Mo par échéance).
AIFS ENS ne publie ni rafales ni Tmax/Tmin sur 6 h : rien n'est inventé à leur place.
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np

MEMBERS = 51
STEPS = list(range(0, 361, 6))
PARAMS = ("2t", "tp")
NI, NJ = 1440, 721
LAT_FIRST, LON_FIRST, PAS = 90.0, -180.0, 0.25

# Contrôles de chaque message GRIB (en plus du run, de l'échéance et du membre).
UNITES = {"2t": "K", "tp": "kg m**-2"}
NIVEAUX = {"2t": ("heightAboveGround", 2), "tp": ("surface", 0)}
TYPES_PAS = {"2t": "instant", "tp": "accum"}


def client():
    from ecmwf.opendata import Client

    return Client(source="ecmwf", model="aifs-ens", resol="0p25", infer_stream_keyword=False)


def latest_run() -> datetime:
    """Run le plus récent dont la dernière échéance (H+360) est publiée."""
    run = client().latest(stream="enfo", type="pf", step=STEPS[-1], param="2t")
    return run.replace(tzinfo=timezone.utc) if run.tzinfo is None else run.astimezone(timezone.utc)


def retrieve(run: datetime, step: int, target: Path, tentatives: int = 3) -> Path:
    """Télécharge les 51 membres d'une échéance (2t + tp) dans `target`."""
    for k in range(tentatives):
        try:
            client().retrieve(
                date=run.strftime("%Y%m%d"),
                time=run.hour,
                stream="enfo",
                type=["cf", "pf"],
                step=step,
                param=list(PARAMS),
                target=str(target),
            )
            if target.stat().st_size < 1_000_000:
                raise OSError(f"fichier H+{step} anormalement petit ({target.stat().st_size} octets)")
            return target
        except Exception as e:  # noqa: BLE001 - coupure passagère : on reprend
            if k == tentatives - 1:
                raise
            attente = 30 * (k + 1)
            print(f"H+{step} : téléchargement à reprendre ({e}) ; attente {attente} s", flush=True)
            time.sleep(attente)
    raise AssertionError("inaccessible")


def grid_index(lat: float, lon: float) -> tuple[int, float, float]:
    """Point de grille le plus proche : indice dans le champ global, latitude et longitude du point."""
    row = min(NJ - 1, max(0, int(round((LAT_FIRST - lat) / PAS))))
    col = int(round(((lon - LON_FIRST) % 360.0) / PAS)) % NI
    return row * NI + col, LAT_FIRST - row * PAS, LON_FIRST + col * PAS


def check_message(get: Callable[[str], Any], run: datetime, step: int) -> tuple[str, int]:
    """Vérifie un message (grille, run, échéance, unité, niveau) ; renvoie (paramètre, membre)."""
    name = str(get("shortName"))
    if name not in PARAMS:
        raise ValueError(f"H+{step} : champ inattendu {name!r}")
    number = int(get("number"))
    lon_first = (float(get("longitudeOfFirstGridPointInDegrees")) + 180.0) % 360.0 - 180.0
    attendu = {
        "Ni": NI,
        "Nj": NJ,
        "jScansPositively": 0,
        "numberOfForecastsInEnsemble": MEMBERS,
        "dataDate": int(run.strftime("%Y%m%d")),
        "dataTime": run.hour * 100,
        "endStep": step,
        "startStep": 0 if name == "tp" else step,
        "stepType": TYPES_PAS[name],
        "units": UNITES[name],
        "typeOfLevel": NIVEAUX[name][0],
        "level": NIVEAUX[name][1],
        "dataType": "cf" if number == 0 else "pf",
    }
    for cle, valeur in attendu.items():
        recu = get(cle)
        if recu != valeur:
            raise ValueError(f"{name} membre {number} H+{step} : {cle} = {recu!r} au lieu de {valeur!r}")
    if not math.isclose(float(get("latitudeOfFirstGridPointInDegrees")), LAT_FIRST, abs_tol=1e-6) or not math.isclose(lon_first, LON_FIRST, abs_tol=1e-6):
        raise ValueError(f"{name} membre {number} H+{step} : grille inattendue")
    if not math.isclose(float(get("iDirectionIncrementInDegrees")), PAS, abs_tol=1e-9):
        raise ValueError(f"{name} membre {number} H+{step} : pas de grille inattendu")
    if not 0 <= number < MEMBERS:
        raise ValueError(f"{name} H+{step} : membre {number} hors de 0-{MEMBERS - 1}")
    return name, number


def decode(path: Path, run: datetime, step: int, indexes: list[int]) -> dict[tuple[str, int], np.ndarray]:
    """Valeurs aux points demandés, par (paramètre, membre) ; échoue si un membre ou un champ manque."""
    from eccodes import codes_get, codes_get_double_elements, codes_grib_new_from_file, codes_release

    out: dict[tuple[str, int], np.ndarray] = {}
    with path.open("rb") as f:
        while True:
            gid = codes_grib_new_from_file(f)
            if gid is None:
                break
            try:
                cle = check_message(lambda k: codes_get(gid, k), run, step)
                if cle in out:
                    raise ValueError(f"H+{step} : message en double {cle}")
                valeurs = np.asarray(codes_get_double_elements(gid, "values", indexes), dtype=np.float64)
                manquant = float(codes_get(gid, "missingValue"))
                if not np.isfinite(valeurs).all() or np.isclose(valeurs, manquant).any():
                    raise ValueError(f"H+{step} : valeurs manquantes pour {cle}")
                out[cle] = valeurs
            finally:
                codes_release(gid)
    attendus = {(p, m) for p in PARAMS for m in range(MEMBERS)}
    if set(out) != attendus:
        manque = sorted(attendus - set(out))[:5]
        raise ValueError(f"H+{step} : {len(attendus - set(out))} messages manquants, par ex. {manque}")
    return out
