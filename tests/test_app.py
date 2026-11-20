import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as appmod  # noqa: E402

FAKE_ELEMENTS = [
    {"type": "node", "id": 1, "lat": 17.386, "lon": 78.480,
     "tags": {"amenity": "blood_donation", "name": "City Blood Bank", "phone": "+91 40 1234 5678",
              "addr:street": "Main Rd", "addr:city": "Hyderabad"}},
    {"type": "way", "id": 2, "center": {"lat": 17.400, "lon": 78.490},
     "tags": {"amenity": "hospital", "name": "General Hospital"}},
    {"type": "node", "id": 3, "lat": 17.5, "lon": 78.5, "tags": {"amenity": "cafe", "name": "Not relevant"}},
    {"type": "node", "id": 4, "lat": 17.386, "lon": 78.480,
     "tags": {"amenity": "blood_donation", "name": "City Blood Bank"}},  # duplicate
]


class AppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.story_file = os.path.join(self.tmp, "stories.json")
        appmod.STORY_FILE = self.story_file
        appmod.BASE_DIR = self.tmp
        appmod._cache.clear()
        appmod._rate.clear()
        self.client = appmod.app.test_client()

    def test_index_served(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Smart Donation Recommender", r.data)
        r.close()

    def test_static_assets_served(self):
        for path in ("/static/js/app.js", "/static/css/style.css"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)
            r.close()

    def test_places_validation(self):
        self.assertEqual(self.client.get("/api/places").status_code, 400)
        self.assertEqual(self.client.get("/api/places?type=gold&lat=1&lon=1").status_code, 400)
        self.assertEqual(self.client.get("/api/places?type=blood&lat=999&lon=1").status_code, 400)
        self.assertEqual(self.client.get("/api/places?type=blood&lat=1&lon=1&radius=500").status_code, 400)

    def test_places_ranking_dedupe_and_cache(self):
        with mock.patch.object(appmod, "query_overpass", return_value=FAKE_ELEMENTS) as q:
            url = "/api/places?type=blood&lat=17.385&lon=78.486&radius=10"
            data = self.client.get(url).get_json()
            self.assertEqual(data["count"], 2)  # cafe dropped, duplicate removed
            self.assertEqual(data["places"][0]["name"], "City Blood Bank")
            self.assertEqual(data["places"][0]["address"], "Main Rd, Hyderabad")
            self.assertTrue(data["places"][0]["best_match"])
            self.assertEqual(data["places"][1]["priority"], 1)
            self.assertFalse(data["cached"])
            self.assertTrue(self.client.get(url).get_json()["cached"])
            self.assertEqual(q.call_count, 1)

    def test_places_upstream_failure(self):
        with mock.patch.object(appmod, "query_overpass", side_effect=RuntimeError("down")):
            r = self.client.get("/api/places?type=books&lat=1&lon=1&radius=5")
            self.assertEqual(r.status_code, 503)

    def test_overpass_query_covers_every_type(self):
        for t in appmod.CATEGORIES:
            q = appmod.build_overpass_query(t, 1.0, 2.0, 5000)
            self.assertIn("around:5000,1.0,2.0", q)
            self.assertTrue(q.rstrip().endswith(";"))

    def test_story_flow(self):
        good = {"story": "I donated blood last week and it felt wonderful to help someone in need.", "name": "Asha", "type": "blood"}
        r = self.client.post("/api/stories", json=good)
        self.assertEqual(r.status_code, 201)
        stories = self.client.get("/api/stories").get_json()["stories"]
        self.assertEqual(stories[0]["name"], "Asha")
        with open(self.story_file) as f:
            self.assertEqual(len(json.load(f)), 1)

    def test_story_validation(self):
        self.assertEqual(self.client.post("/api/stories", json={"story": "too short"}).status_code, 400)
        self.assertEqual(self.client.post("/api/stories", json={"story": "word " * 101}).status_code, 400)
        self.assertEqual(self.client.post("/api/stories", data="not json").status_code, 400)

    def test_story_rate_limit(self):
        s = {"story": "word " * 12}
        codes = [self.client.post("/api/stories", json=s).status_code for _ in range(6)]
        self.assertEqual(codes[:5], [201] * 5)
        self.assertEqual(codes[5], 429)

    def test_corrupt_story_file_is_tolerated(self):
        with open(self.story_file, "w") as f:
            f.write("{broken")
        self.assertEqual(self.client.get("/api/stories").get_json()["stories"], [])
        self.assertEqual(self.client.post("/api/stories", json={"story": "word " * 12}).status_code, 201)

    def test_geocode(self):
        fake = mock.Mock()
        fake.raise_for_status.return_value = None
        fake.json.return_value = [{"display_name": "Hyderabad, Telangana, India", "lat": "17.38", "lon": "78.48"}]
        with mock.patch.object(appmod.requests, "get", return_value=fake):
            data = self.client.get("/api/geocode?q=Hyderabad").get_json()
        self.assertEqual(data["results"][0]["lat"], 17.38)
        self.assertEqual(self.client.get("/api/geocode?q=a").status_code, 400)


if __name__ == "__main__":
    unittest.main()
