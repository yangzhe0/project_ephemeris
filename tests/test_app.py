import sys
from pathlib import Path

# Ensure project root is in sys.path regardless of where tests are run from
project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

import unittest
from unittest.mock import MagicMock, patch

import app as ephemeris

SAMPLE_PRE = """
 header
 2026 09 10 04 05 06.0 12 34 56.78 -23 45 01.2 extra
 footer
"""

SAMPLE_TRITON_PRE = """
v5.21.05i     Planet: Neptune  (DE441)
        Satellite: N1 Triton  (Emelyanov, Samorodov (2015) MNRAS, 454, 2205. )
    Observatory N: 286 - Yunnan Observatory
        Timescale: UTC
Mean equator and equinox of J2000.  ICRF. 
Coordinates astrometric 
Year  M  D   H  M  S     alpha (h m s)      delta (° ' ")
2026 11  5  12  0  0.00  0  8 10.224206    0 -39  24.875698
2026 11  5  13  0  0.00  0  8 09.812345   -0  39  25.123456
"""


class EphemerisTests(unittest.TestCase):
    def setUp(self):
        ephemeris.app.config.update(TESTING=True)
        self.client = ephemeris.app.test_client()

    def test_index_and_favicon(self):
        index = self.client.get("/")
        self.assertEqual(index.status_code, 200)
        self.assertIn(b"fullscreenTargetMarker", index.data)
        favicon = self.client.get("/favicon.ico")
        self.assertEqual(favicon.status_code, 200)
        self.assertEqual(favicon.mimetype, "image/jpeg")
        favicon.close()

    def test_preset_satellites_cover_tonight_outline(self):
        """Ensure all required satellites from observation outline are present."""
        codes = {s["code"] for s in ephemeris.SATELLITES}
        required_codes = [
            "8001",  # N1 Triton
            "8002",  # N2 Nereid
            "6008",  # S8 Iapetus
            "6009",  # S9 Phoebe
            "6001",  # S1~S7 (Mimas to Hyperion in same 10' FOV)
            "7001",  # U1~U5 (Miranda to Oberon in same FOV)
            "10001", "10002", "10003", "10004",  # J6-J9
        ]
        self.assertEqual(len(ephemeris.SATELLITES), 10)
        for c in required_codes:
            self.assertIn(c, codes, f"Required satellite code {c} is missing from presets!")

    def test_parse_ephemeris(self):
        rows = ephemeris.parse_ephemeris(SAMPLE_PRE)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["time"], "2026-09-10 04:05:06")
        self.assertEqual(rows[0]["ra_pure"], "12 34 56.78")
        self.assertEqual(rows[0]["de_pure"], "-23 45 01.2")

    def test_parse_ephemeris_near_equator_negative_dec(self):
        """Regression test for Triton N1 negative Dec where degree is 0 or -0 and minus is on minutes."""
        rows = ephemeris.parse_ephemeris(SAMPLE_TRITON_PRE)
        self.assertEqual(len(rows), 2)
        # First row: "0 -39 24.875698" -> "-0 39 24.875698"
        self.assertEqual(rows[0]["time"], "2026-11-05 12:00:00")
        self.assertEqual(rows[0]["ra_pure"], "0 8 10.224206")
        self.assertEqual(rows[0]["de_pure"], "-0 39 24.875698")
        self.assertEqual(rows[0]["de"], "-0° 39' 24.875698\"")

        # Second row: "-0 39 25.123456" -> "-0 39 25.123456"
        self.assertEqual(rows[1]["time"], "2026-11-05 13:00:00")
        self.assertEqual(rows[1]["de_pure"], "-0 39 25.123456")

    def test_parse_ephemeris_skips_invalid_coordinate(self):
        self.assertEqual(
            ephemeris.parse_ephemeris("2026 09 10 04 05 06 25 00 00 00 00 00"),
            [],
        )

    def test_calculate_rejects_non_json_and_bad_values(self):
        response = self.client.post("/api/calculate", data="text", content_type="text/plain")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json["success"])

        payload = {
            "satellite": "6000", "observatory": "286",
            "initmom": "2026 02 30 12 00 00", "ntimes": 1,
        }
        self.assertEqual(self.client.post("/api/calculate", json=payload).status_code, 400)
        payload["initmom"] = "2026 09 10 12 00 00"
        payload["ntimes"] = 1000
        self.assertEqual(self.client.post("/api/calculate", json=payload).status_code, 400)

    @patch("app.fetch_satellite_data")
    def test_calculate_accepts_custom_satellite_code(self, fetch):
        fetch.return_value = {"success": True, "data": []}
        response = self.client.post("/api/calculate", json={
            "satellite": "5001", "observatory": "286",
            "initmom": "2026 09 10 12 00 00", "nde": "6",
            "ntimes": "2", "timestep": "0.5",
        })
        self.assertEqual(response.status_code, 200)
        fetch.assert_called_once_with("0", "5001", "6", "286", "2026 09 10 12 00 00", "2", "0.5")

    @patch("app.requests.post")
    def test_msu_rejected_custom_code_has_clear_error(self, post):
        response = MagicMock()
        response.text = "<html><head><title>NatSatEphemOutput - Error</title></head><body><pre>N 7008</pre></body></html>"
        response.raise_for_status.return_value = None
        post.return_value = response
        result = ephemeris.fetch_satellite_data(
            "0", "301", "6", "286", "2026 09 10 12 00 00", "1", "1"
        )
        self.assertFalse(result["success"])
        self.assertIn("不支持天体代码 301", result["message"])

    def test_chart_rejects_invalid_coordinates_and_fov(self):
        response = self.client.post("/api/download_chart", json={
            "ra": "99 00 00", "dec": "0 0 0", "fov": 10,
        })
        self.assertEqual(response.status_code, 400)
        response = self.client.post("/api/download_chart", json={
            "ra": "12 00 00", "dec": "0 0 0", "fov": 100,
        })
        self.assertEqual(response.status_code, 400)

    def test_arbitrary_file_path_is_not_exposed(self):
        response = self.client.get("/api/get_file?path=C:/Windows/win.ini&filename=test.txt")
        self.assertEqual(response.status_code, 404)
        response = self.client.get("/api/get_file/not-a-cache-id")
        self.assertEqual(response.status_code, 404)

    @patch("app.requests.get")
    def test_chart_download_cache_and_safe_filename(self, get):
        fake_response = MagicMock()
        fake_response.headers = {"content-type": "image/gif", "content-length": "6"}
        fake_response.iter_content.return_value = [b"GIF89a"]
        fake_response.raise_for_status.return_value = None
        get.return_value = fake_response

        ra, dec, fov = "01 02 03", "+04 05 06", 11.25
        cache_id = ephemeris._cache_id(ra, dec, fov, fov, "poss2ukstu_red", "gif")
        cache_path = ephemeris._cache_path(cache_id)
        cache_path.unlink(missing_ok=True)
        self.addCleanup(cache_path.unlink, missing_ok=True)

        response = self.client.post("/api/download_chart", json={
            "ra": ra, "dec": dec, "fov": fov,
            "satellite_name": "../../unsafe:name", "time_str": "2026-09-10 04:05:06",
        })
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("path=", response.json["download_url"])
        self.assertNotIn("..", response.json["filename"])

        downloaded = self.client.get(response.json["download_url"])
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.data, b"GIF89a")
        downloaded.close()



    def test_observation_status_dynamic_endpoint(self):
        """Verify dynamic astronomical calculation endpoint computes real dark sky & windows."""
        res = self.client.get("/api/observation_status?obs=286&time=2026-11-05 21:00:00")
        self.assertEqual(res.status_code, 200)
        data = res.json
        self.assertTrue(data["success"])
        self.assertEqual(data["night_date"], "2026-11-05")
        self.assertIn("twilight", data)
        self.assertIn("planets", data)
        # Neptune and Saturn should be active around 21:00 on Nov 5
        self.assertTrue(data["planets"]["neptune"]["valid"])
        self.assertEqual(data["planets"]["neptune"]["status"], "observing")
        self.assertGreater(data["planets"]["neptune"]["pct"], 0)

    def test_calculate_alt_az_for_kunming(self):
        """Verify altitude calculation relative to Yunnan Observatory Kunming 1m station."""
        from datetime import datetime
        stamp = datetime(2026, 11, 5, 12, 0, 0)
        res = ephemeris.calculate_alt_az(stamp, "0 8 10.22", "-0 39 24.88", obs_code="286")
        self.assertIn("alt_deg", res)
        self.assertIn("alt_str", res)
        self.assertGreater(res["alt_deg"], 40.0)
        self.assertTrue(res["is_observable"])
        self.assertEqual(res["obs_status"], "observable")
        self.assertIn("云南天文台昆明", res["station_name"])

        # Test geocentric code 500 defaults to Yunnan Observatory Kunming 1m for altitude
        res_geo = ephemeris.calculate_alt_az(stamp, "0 8 10.22", "-0 39 24.88", obs_code="500")
        self.assertEqual(res_geo["station_code"], "286")

    def test_target_metadata_and_diameter_in_ephemeris(self):
        """Verify satellite diameters and angular sizes are included in ephemeris output."""
        self.assertIn("8001", ephemeris.TARGET_METADATA)
        self.assertIn("2,707 km", ephemeris.TARGET_METADATA["8001"]["diameter"])
        self.assertIn("0.13″", ephemeris.TARGET_METADATA["8001"]["angular_size"])

        rows = ephemeris.parse_ephemeris(SAMPLE_TRITON_PRE, observatory="286", satellite="8001")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["diameter"], "2,707 km")
        self.assertIn("0.13″", rows[0]["angular_size"])
        self.assertIn("alt_deg", rows[0])
        self.assertIn("alt_str", rows[0])
        self.assertIn("云南天文台昆明", rows[0]["station_name"])


if __name__ == "__main__":
    unittest.main()
