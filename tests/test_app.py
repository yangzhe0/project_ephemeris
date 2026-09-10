import unittest
from unittest.mock import MagicMock, patch

import app as ephemeris


SAMPLE_PRE = """
 header
 2026 09 10 04 05 06.0 12 34 56.78 -23 45 01.2 extra
 footer
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

    def test_parse_ephemeris(self):
        rows = ephemeris.parse_ephemeris(SAMPLE_PRE)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["time"], "2026-09-10 04:05:06")
        self.assertEqual(rows[0]["ra_pure"], "12 34 56.78")
        self.assertEqual(rows[0]["de_pure"], "-23 45 01.2")

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


if __name__ == "__main__":
    unittest.main()
