"""Smart Donation Recommender - Flask backend.

Endpoints
---------
GET  /                  -> the web app
GET  /api/places        -> real-time donation places from OpenStreetMap (Overpass API)
GET  /api/geocode       -> turn a typed place name into coordinates (Nominatim)
GET  /api/stories       -> latest donation stories
POST /api/stories       -> submit a donation story
"""
import json
import math
import os
import re
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, request, send_from_directory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STORY_FILE = os.path.join(BASE_DIR, "stories.json")

app = Flask(__name__, static_folder="static", static_url_path="/static")

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
# OpenStreetMap services require an identifying User-Agent.
USER_AGENT = os.environ.get(
    "APP_USER_AGENT", "SmartDonationRecommender/1.0 (student project)"
)
CACHE_TTL = 300  # seconds
MAX_RADIUS_KM = 50
MAX_STORY_WORDS = 100
MIN_STORY_WORDS = 10

# --------------------------------------------------------------------------
# What to look for on the map, per donation type.
# priority 0 = dedicated place, 1 = general place that may accept donations.
# --------------------------------------------------------------------------
CATEGORIES = {
    "blood": [
        {"tags": {"amenity": "blood_donation"}, "label": "Blood donation centre", "priority": 0},
        {"tags": {"healthcare": "blood_donation"}, "label": "Blood donation centre", "priority": 0},
        {"tags": {"healthcare": "blood_bank"}, "label": "Blood bank", "priority": 0},
        {"tags": {"amenity": "hospital"}, "label": "Hospital (confirm blood donation hours)", "priority": 1},
    ],
    "books": [
        {"tags": {"amenity": "public_bookcase"}, "label": "Public bookcase", "priority": 0},
        {"tags": {"amenity": "library"}, "label": "Library", "priority": 0},
        {"tags": {"amenity": "social_facility", "social_facility:for": "orphan"}, "label": "Orphanage / children's home", "priority": 0},
        {"tags": {"amenity": "orphanage"}, "label": "Orphanage / children's home", "priority": 0},
        {"tags": {"shop": "charity"}, "label": "Charity shop", "priority": 1},
    ],
    "clothes": [
        {"tags": {"amenity": "recycling", "recycling:clothes": "yes"}, "label": "Clothes donation bin", "priority": 0},
        {"tags": {"social_facility": "clothing_bank"}, "label": "Clothing bank", "priority": 0},
        {"tags": {"shop": "charity"}, "label": "Charity shop", "priority": 0},
        {"tags": {"amenity": "social_facility", "social_facility": "shelter"}, "label": "Shelter", "priority": 1},
        {"tags": {"amenity": "social_facility", "social_facility:for": "orphan"}, "label": "Orphanage / children's home", "priority": 1},
        {"tags": {"amenity": "orphanage"}, "label": "Orphanage / children's home", "priority": 1},
    ],
    "time": [
        {"tags": {"office": "ngo"}, "label": "NGO", "priority": 0},
        {"tags": {"office": "charity"}, "label": "Charity", "priority": 0},
        {"tags": {"social_facility": "food_bank"}, "label": "Food bank", "priority": 0},
        {"tags": {"amenity": "social_facility"}, "label": "Social facility", "priority": 1},
        {"tags": {"amenity": "community_centre"}, "label": "Community centre", "priority": 1},
        {"tags": {"office": "association"}, "label": "Association", "priority": 1},
    ],
}
# A priority-1 place counts as being this many km "farther away" when ranking,
# so dedicated places beat generic ones unless the generic one is much closer.
PRIORITY_PENALTY_KM = 3.0

_cache = {}
_cache_lock = threading.Lock()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def cache_get(key):
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL:
            return hit[1]
        _cache.pop(key, None)
    return None


def cache_set(key, value):
    with _cache_lock:
        if len(_cache) > 300:
            oldest = min(_cache, key=lambda k: _cache[k][0])
            _cache.pop(oldest, None)
        _cache[key] = (time.time(), value)


def build_overpass_query(donation_type, lat, lon, radius_m):
    parts = []
    for f in CATEGORIES[donation_type]:
        filters = "".join(f'["{k}"="{v}"]' for k, v in f["tags"].items())
        parts.append(f"  nwr{filters}(around:{radius_m},{lat},{lon});")
    body = "\n".join(parts)
    return f"[out:json][timeout:25];\n(\n{body}\n);\nout center tags 250;"


def classify(donation_type, tags):
    """Return (label, priority) of the first matching category, else None."""
    for f in CATEGORIES[donation_type]:
        if all(tags.get(k) == v for k, v in f["tags"].items()):
            return f["label"], f["priority"]
    return None


def format_address(tags):
    street = " ".join(x for x in (tags.get("addr:housenumber"), tags.get("addr:street")) if x)
    area = tags.get("addr:suburb") or tags.get("addr:neighbourhood")
    city = tags.get("addr:city") or tags.get("addr:town") or tags.get("addr:village")
    parts = [p for p in (street, area, city, tags.get("addr:postcode")) if p]
    return ", ".join(parts)


def parse_places(donation_type, elements, lat, lon):
    places, seen = [], set()
    for el in elements:
        tags = el.get("tags") or {}
        cls = classify(donation_type, tags)
        if not cls:
            continue
        if "lat" in el and "lon" in el:
            plat, plon = el["lat"], el["lon"]
        elif el.get("center"):
            plat, plon = el["center"]["lat"], el["center"]["lon"]
        else:
            continue
        label, priority = cls
        name = tags.get("name") or tags.get("name:en") or tags.get("operator") or label
        # Skip duplicates (same name at ~same spot, e.g. a way and its node).
        dedupe = (name.lower(), round(plat, 3), round(plon, 3))
        if dedupe in seen:
            continue
        seen.add(dedupe)
        dist = haversine_km(lat, lon, plat, plon)
        places.append(
            {
                "id": f'{el.get("type", "node")}/{el.get("id")}',
                "name": name,
                "category": label,
                "priority": priority,
                "lat": plat,
                "lon": plon,
                "distance_km": round(dist, 2),
                "address": format_address(tags),
                "phone": tags.get("phone") or tags.get("contact:phone") or "",
                "website": tags.get("website") or tags.get("contact:website") or "",
                "opening_hours": tags.get("opening_hours", ""),
            }
        )
    places.sort(key=lambda p: p["distance_km"] + p["priority"] * PRIORITY_PENALTY_KM)
    for i, p in enumerate(places):
        p["best_match"] = i < 3
    return places


def query_overpass(query):
    last_error = None
    for url in OVERPASS_URLS:
        try:
            resp = requests.post(
                url,
                data={"data": query},
                headers={"User-Agent": USER_AGENT},
                timeout=30,
            )
            if resp.status_code == 200:
                return resp.json().get("elements", [])
            last_error = f"{url} returned HTTP {resp.status_code}"
        except (requests.RequestException, ValueError) as exc:
            last_error = f"{url}: {exc}"
    raise RuntimeError(last_error or "Overpass unavailable")


def parse_float(name, lo, hi):
    try:
        value = float(request.args.get(name, ""))
    except ValueError:
        return None
    return value if lo <= value <= hi else None


# --------------------------------------------------------------------------
# Routes: pages and places
# --------------------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/places")
def api_places():
    donation_type = request.args.get("type", "").lower()
    if donation_type not in CATEGORIES:
        return jsonify(error="Choose a donation type: " + ", ".join(CATEGORIES)), 400
    lat = parse_float("lat", -90, 90)
    lon = parse_float("lon", -180, 180)
    radius_km = parse_float("radius", 0.5, MAX_RADIUS_KM)
    if radius_km is None:
        radius_km = 10.0 if request.args.get("radius") is None else None
    if lat is None or lon is None or radius_km is None:
        return jsonify(error=f"Provide valid lat, lon and a radius between 0.5 and {MAX_RADIUS_KM} km."), 400

    key = (donation_type, round(lat, 3), round(lon, 3), radius_km)
    places = cache_get(key)
    cached = places is not None
    if not cached:
        query = build_overpass_query(donation_type, lat, lon, int(radius_km * 1000))
        try:
            elements = query_overpass(query)
        except RuntimeError as exc:
            app.logger.warning("Overpass failed: %s", exc)
            return jsonify(error="The map data service is busy. Please try again in a moment."), 503
        places = parse_places(donation_type, elements, lat, lon)
        cache_set(key, places)

    return jsonify(type=donation_type, count=len(places), cached=cached, places=places)


def _parse_nominatim(data):
    return [{"name": r["display_name"], "lat": float(r["lat"]), "lon": float(r["lon"])} for r in data]


def _parse_open_meteo(data):
    out = []
    for r in data.get("results", []):
        name = ", ".join(x for x in (r.get("name"), r.get("admin1"), r.get("country")) if x)
        out.append({"name": name, "lat": float(r["latitude"]), "lon": float(r["longitude"])})
    return out


def _parse_photon(data):
    out = []
    for f in data.get("features", []):
        p, (lon, lat) = f.get("properties", {}), f["geometry"]["coordinates"]
        name = ", ".join(x for x in (p.get("name"), p.get("state"), p.get("country")) if x)
        out.append({"name": name, "lat": float(lat), "lon": float(lon)})
    return out


GEOCODERS = [
    ("nominatim", NOMINATIM_URL, lambda q: {"q": q, "format": "jsonv2", "limit": 5}, _parse_nominatim),
    ("open-meteo", "https://geocoding-api.open-meteo.com/v1/search",
     lambda q: {"name": q, "count": 5, "format": "json"}, _parse_open_meteo),
    ("photon", "https://photon.komoot.io/api/", lambda q: {"q": q, "limit": 5}, _parse_photon),
]


@app.route("/api/geocode")
def api_geocode():
    q = (request.args.get("q") or "").strip()
    if len(q) < 2 or len(q) > 120:
        return jsonify(error="Enter a city, area or address."), 400
    key = ("geo", q.lower())
    results = cache_get(key)
    if results is None:
        for name, url, params, parse in GEOCODERS:
            try:
                resp = requests.get(url, params=params(q), headers={"User-Agent": USER_AGENT}, timeout=10)
                resp.raise_for_status()
                results = parse(resp.json())
                if results:
                    break
            except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
                app.logger.warning("Geocoder %s failed: %s", name, exc)
        if results is None:
            return jsonify(error="Could not look up that place. Clear the box to use your current location instead."), 503
        cache_set(key, results)
    return jsonify(results=results)

# --------------------------------------------------------------------------
# Routes: stories
# --------------------------------------------------------------------------
_story_lock = threading.Lock()
_rate = {}
RATE_LIMIT = 5  # stories
RATE_WINDOW = 600  # per 10 minutes, per IP


def load_stories():
    if not os.path.exists(STORY_FILE):
        return []
    try:
        with open(STORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_stories(stories):
    fd, tmp = tempfile.mkstemp(dir=BASE_DIR, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(stories, f, indent=2, ensure_ascii=False)
    os.replace(tmp, STORY_FILE)  # atomic: never leaves a half-written file


def rate_limited(ip):
    now = time.time()
    recent = [t for t in _rate.get(ip, []) if now - t < RATE_WINDOW]
    if len(recent) >= RATE_LIMIT:
        _rate[ip] = recent
        return True
    recent.append(now)
    _rate[ip] = recent
    return False


def clean(text, limit):
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


@app.route("/api/stories", methods=["GET"])
def get_stories():
    with _story_lock:
        stories = load_stories()
    public = [
        {k: s.get(k, "") for k in ("id", "name", "story", "type", "created_at")}
        for s in stories[-20:]
    ]
    return jsonify(stories=list(reversed(public)))


@app.route("/api/stories", methods=["POST"])
def post_story():
    data = request.get_json(silent=True) or {}
    story = clean(data.get("story"), 1500)
    words = len(story.split())
    if words < MIN_STORY_WORDS:
        return jsonify(success=False, error=f"Please write at least {MIN_STORY_WORDS} words."), 400
    if words > MAX_STORY_WORDS:
        return jsonify(success=False, error=f"Please keep your story to {MAX_STORY_WORDS} words."), 400
    dtype = str(data.get("type") or "")
    if dtype not in CATEGORIES:
        dtype = ""
    if rate_limited(request.remote_addr or "unknown"):
        return jsonify(success=False, error="Too many stories submitted. Try again in a few minutes."), 429

    entry = {
        "id": uuid.uuid4().hex[:12],
        "name": clean(data.get("name"), 40) or "Anonymous",
        "story": story,
        "type": dtype,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    with _story_lock:
        stories = load_stories()
        stories.append(entry)
        save_stories(stories)
    return jsonify(success=True, story=entry), 201


if __name__ == "__main__":
    app.run(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", 5000)),
        debug=os.environ.get("FLASK_DEBUG") == "1",
    )
