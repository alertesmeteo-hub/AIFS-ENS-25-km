import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_data  # noqa: E402
from source import MEMBERS, NI, check_message, grid_index  # noqa: E402

RUN = datetime(2026, 10, 12, 6, tzinfo=timezone.utc)


def message(**surcharge):
    """Clés d'un message 2t du membre 3 à H+24 du run 06 UTC (tel que lu dans les vrais fichiers)."""
    m = {
        "shortName": "2t", "number": 3, "dataType": "pf", "Ni": 1440, "Nj": 721, "jScansPositively": 0,
        "numberOfForecastsInEnsemble": 51, "dataDate": 20261012, "dataTime": 600, "startStep": 24, "endStep": 24,
        "stepType": "instant", "units": "K", "typeOfLevel": "heightAboveGround", "level": 2,
        "latitudeOfFirstGridPointInDegrees": 90.0, "longitudeOfFirstGridPointInDegrees": 180.0, "iDirectionIncrementInDegrees": 0.25,
    }
    m.update(surcharge)
    return m.__getitem__


class Grille(unittest.TestCase):
    def test_perpignan_tombe_sur_la_maille_la_plus_proche(self):
        idx, lat, lon = grid_index(42.699, 2.9045)
        self.assertEqual((lat, lon), (42.75, 3.0))
        self.assertEqual(idx, 189 * NI + 732)

    def test_longitudes_ouest(self):
        _, _, lon = grid_index(48.39, -4.49)  # Brest
        self.assertEqual(lon, -4.5)


class Messages(unittest.TestCase):
    def test_message_conforme(self):
        self.assertEqual(check_message(message(), RUN, 24), ("2t", 3))

    def test_pluie_cumulee_depuis_le_run(self):
        m = message(shortName="tp", units="kg m**-2", stepType="accum", startStep=0, typeOfLevel="surface", level=0)
        self.assertEqual(check_message(m, RUN, 24), ("tp", 3))

    def test_refus(self):
        for cle, valeur in [("dataTime", 0), ("endStep", 30), ("units", "C"), ("Ni", 720), ("numberOfForecastsInEnsemble", 31),
                            ("dataType", "cf"), ("longitudeOfFirstGridPointInDegrees", 0.0), ("shortName", "10u")]:
            with self.subTest(cle=cle), self.assertRaises(ValueError):
                check_message(message(**{cle: valeur}), RUN, 24)

    def test_controle_membre_zero(self):
        self.assertEqual(check_message(message(number=0, dataType="cf"), RUN, 24), ("2t", 0))


class Statistiques(unittest.TestCase):
    def test_quantiles_sur_les_membres(self):
        v = np.arange(MEMBERS, dtype=float).reshape(MEMBERS, 1)
        s = build_data.statistiques(v)
        self.assertAlmostEqual(s["mean"][0], 25.0)
        self.assertAlmostEqual(s["median"][0], 25.0)
        self.assertAlmostEqual(s["p10"][0], 5.0)
        self.assertAlmostEqual(s["p90"][0], 45.0)

    def test_membre_manquant_refuse(self):
        with self.assertRaises(ValueError):
            build_data.statistiques(np.zeros((MEMBERS - 1, 3)))
        v = np.zeros((MEMBERS, 2))
        v[4, 1] = np.nan
        with self.assertRaises(ValueError):
            build_data.statistiques(v)

    def test_pluie_6h_depuis_le_cumul(self):
        total = np.array([[[0.0], [2.0], [5.0], [5.0]]])  # 1 membre, 4 échéances, 1 point
        p = build_data.pluie_6h(total)
        self.assertTrue(np.isnan(p[0, 0, 0]))
        self.assertEqual(p[0, 1:, 0].tolist(), [2.0, 3.0, 0.0])

    def test_lignes_dans_l_ordre_des_colonnes(self):
        stats = {
            "temperature": {"mean": np.array([[12.345]])},
            "pluie": {"mean": np.array([[np.nan]])},
            "cumul": {"mean": np.array([[0.0]])},
        }
        self.assertEqual(build_data.lignes(stats, "mean", 0, [0]), [[12.35, None, 0.0]])
        self.assertEqual(build_data.COLONNES, ["temperature_c", "precipitation_mm", "precipitation_total_mm"])


class Membres(unittest.TestCase):
    def test_membres_publies_tels_quels_dans_l_ordre_du_departement(self):
        import json
        import tempfile

        from source import STEPS

        # 3 points de grille dans le catalogue ; le 66 n'en utilise que 2 (indices 30 puis 10 dans l'ordre des communes)
        indexes = [10, 20, 30]
        points = {10: (42.5, 2.75), 20: (43.0, 1.0), 30: (42.75, 3.0)}
        c1 = ["66136", "Perpignan", "66", ["66000"], 121616, 42.699, 2.9045]
        c2 = ["66025", "Bourg-Madame", "66", ["66760"], 1300, 42.43, 1.94]
        par_dep = {"66": [(c1, 30), (c2, 10)], "11": [(["11069", "Carcassonne", "11", ["11000"], 46000, 43.21, 2.35], 20)]}
        temperature = np.zeros((MEMBERS, len(STEPS), 3))
        cumul = np.zeros((MEMBERS, len(STEPS), 3))
        temperature[7, 4, 2] = 21.26  # membre 7, H+24, point 30
        cumul[7, 4:, 2] = 3.456
        with tempfile.TemporaryDirectory() as d:
            build_data.ecrire_membres(RUN, indexes, points, par_dep, temperature, cumul, ["66"], Path(d))
            m = json.loads((Path(d) / "membres" / "66.json").read_text(encoding="utf-8"))
            self.assertFalse((Path(d) / "membres" / "11.json").exists())
        self.assertEqual(m["members"], MEMBERS)
        self.assertEqual(len(m["values"]), MEMBERS)
        self.assertEqual(len(m["times"]), len(STEPS))
        self.assertEqual(m["times"][4], "2026-10-13T06:00:00Z")
        self.assertEqual(m["points"], [[10, 42.5, 2.75, None], [30, 42.75, 3.0, None]])
        self.assertEqual([c[0] for c in m["communes"]], ["66136", "66025"])
        self.assertEqual([c[-1] for c in m["communes"]], [1, 0])  # Perpignan -> point 30, 2e point du département
        self.assertEqual(m["values"][7][4][1], [21.3, 3.46])
        self.assertEqual(m["values"][7][3][1], [0.0, 0.0])
        self.assertEqual(m["values"][0][4][0], [0.0, 0.0])


if __name__ == "__main__":
    unittest.main()
