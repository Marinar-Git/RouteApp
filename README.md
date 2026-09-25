# RouteApp

In-house route planning for Marinar's delivery drivers: dispatcher map, route editor and stop-order optimizer.
Driver app and live GPS tracking come next.

## How routes work

- Each driver has a fixed route of **waves** that start and end at HQ (Marinar EHF, Viðarhöfði 3):
  **D0** fresh rice + empty container pickup, **D1** first shipment + cooler pickups, **D2** evening shipment + last pickups.
- Stops never move between waves. Inside a wave, the optimizer re-orders stops for the shortest drive.
- **Pickup → drop links**: a pickup must be visited before the drops it feeds, in the same wave
  (e.g. Tokyo Nýbýlavegur → Hringbraut Hospital + Hallveigarstígur Krónan). One pickup can feed several drops.

## Run locally

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m uvicorn app.main:app --port 8000 --reload
```

Open http://localhost:8000 — **Map** shows all drivers' routes (current vs optimized), **Edit routes** edits drivers,
waves, stops, pickup links and locations. On first run the database (`data/app.db`) is seeded from `data/route_sample.json`.

## Layout

| Path | What |
|---|---|
| `app/main.py` | FastAPI app: locations/routes API, `/api/optimize`, `/api/plan`, serves `web/` |
| `app/routing.py` | Geocoding (Nominatim, plus codes), OSRM road matrix/geometry, OR-Tools wave optimizer |
| `web/map.html`, `web/edit.html` | Dispatcher map and route editor (plain JS + Leaflet) |
| `prototype.py` | Original one-file prototype (JSON in → HTML map out) |

## Known limitations / next steps

- Road times come from the public OSRM demo server with **no traffic**. Production: self-hosted OSRM (Iceland extract) + a traffic-aware API for ETAs.
- SQLite for now → Azure Database for PostgreSQL when deployed (container via `az acr build`, behind Easy Auth).
- No time windows yet; drag-and-drop in the editor doesn't work on touch devices.
- Planned: driver app (Expo/React Native, background GPS), live tracking, workday analytics.
