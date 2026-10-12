# AIFS ENS 25 km — Alertes Météo

Ensemble IA d'ECMWF (AIFS ENS), en tableaux par commune pour toute la France
(34 746 communes, 96 départements), au même format que les autres ensembles
(PEARP, GEFS, AIGEFS). Utilisé par les plumes d'incertitude d'app.alertes-meteo.com
(via `communes-data` du dépôt harmonie-knmi).

## Source

ECMWF Open Data, modèle `aifs-ens`, flux `enfo` : 51 membres (contrôle 0 et
perturbés 1 à 50), grille 0,25°, H+0 à H+360 par pas de 6 h, runs 00, 06, 12 et
18 UTC (complets environ 7 h 40 après l'heure du run). Licence CC BY 4.0 :
citer « ECMWF ».

Seuls deux champs sont téléchargés, par plages d'octets grâce aux fichiers
`.index` (environ 77 Mo par échéance, 4,7 Go par run) :

- `2t` : température à 2 m (K → °C) ;
- `tp` : pluie cumulée depuis le run (kg/m² = mm).

AIFS ENS ne publie ni rafales ni Tmax/Tmin sur 6 h : ces colonnes n'existent
pas ici, rien n'est inventé à leur place. La Tmax/Tmin du jour lue en aval est
celle des 4 valeurs de 6 h (légère sous-estimation des extrêmes).

## Publication (branche `data`, réécrite à chaque run)

- `index.json` : run (`run` et `model.run_time`), échéances, colonnes, départements ;
- `departements/XX.json` : format « points + communes », schéma 4. Colonnes
  `temperature_c`, `precipitation_mm` (6 h, absente à H+0), `precipitation_total_mm`.
  `forecast` = moyenne des 51 membres ; `forecast_statistics` = `median`, `p10`, `p90`.

Les statistiques sont calculées membre par membre puis sur les 51 membres
(pluie 6 h et cumul calculés pour chaque membre avant les quantiles).

## Production

Workflow `build-aifs-ens.yml`, programmé 4 fois par jour et manuel (option
« force »). Le run le plus récent dont l'échéance H+360 existe est choisi ; s'il
est déjà publié, rien n'est fait. Chaque message GRIB est contrôlé (run,
échéance, membre, grille, unité, niveau, type de cumul, valeurs manquantes) et
`validate_data.py` vérifie toute la publication (quantiles ordonnés, cumuls
croissants, 51 membres, 61 échéances) : toute erreur bloque et laisse la
publication précédente en ligne.

Tests : `python -m unittest discover -s tests`.
