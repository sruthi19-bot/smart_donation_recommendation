# Smart Donation Recommender

Pick what you can donate (blood, books, clothes or time) and see real places near you on a map,
ranked by distance and relevance. Data comes live from OpenStreetMap, so no API keys are needed.

## Run it

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5000 and allow location access (or type a city/address).

Optional environment variables: `PORT`, `HOST`, `FLASK_DEBUG=1`, `APP_USER_AGENT`
(set this to your own app name + contact email, as OpenStreetMap's usage policy asks).

## Test it

```bash
python -m unittest discover -s tests -v
```

## How it works

| Piece | Role |
|---|---|
| `GET /api/places?type=&lat=&lon=&radius=` | Queries the Overpass API for tagged places (blood banks, libraries, charity shops, NGOs...), ranks them, caches for 5 min |
| `GET /api/geocode?q=` | Converts a typed place name to coordinates (Nominatim) |
| `GET/POST /api/stories` | Reads/saves donation stories in `stories.json` (validated, rate-limited, atomic writes) |
| `static/` | Leaflet map UI, results list, blood-group compatibility tip, stories |

## Notes

- Hospitals appear for blood donation as a fallback; they are ranked below dedicated blood banks.
- OpenStreetMap coverage varies by area, so some places may be missing. Always call ahead.
- Don't commit API keys. If you add a paid map provider later, load keys from environment variables.
