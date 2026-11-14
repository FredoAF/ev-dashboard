# EV Dashboard

Tracks running costs for a Renault 5 and compares them with what the same miles would have cost in a petrol car. Two cron jobs write to Redis; Grafana reads Redis for the dashboard. Runs on a small k3s box.

```
gov.uk Fuel Finder ─► fuel_price.py ─► pricePerLitre ─┐
Renault API ────────► renault.py ────► stats/streams ──┴─► Redis ─► Grafana
```

## Jobs

Both jobs use the same image (`ghcr.io/fredoaf/ev-dashboard`). It runs `renault.py` by default; to run the fuel job, override the command with `python /app/fuel_price.py`.

| Script | Schedule | What it does |
|---|---|---|
| `renault.py` | every 3 hours | Battery level and range, new charges (costed by EDF day/night rate), mileage, and running electric vs petrol totals |
| `fuel_price.py` | daily | E10 price at Wolverton Tesco from the Fuel Finder API |

Failures send an alert to ntfy topic `farren-renault`.

## Configuration

Environment variables (or a local `.env`):

| Variable | Used by | Notes |
|---|---|---|
| `REDIS_HOST` | both | default `localhost` |
| `RENAULT_USERNAME`, `RENAULT_PASSWORD`, `RENAULT_ACCOUNT_ID`, `RENAULT_VIN` | renault.py | MyRenault account |
| `ONEGOV_CLIENT_ID`, `ONEGOV_CLIENT_SECRET` | fuel_price.py | Fuel Finder API credentials |
| `TARIFF_TZ` | renault.py | timezone for the off-peak window, default `Europe/London` (use `UTC` if the meter keeps GMT all year) |
| `LOG_LEVEL` | both | default `INFO` |
| `LOG_FORMAT` | both | Python logging format, default `%(levelname)-7s %(message)s` |

Settings read from Redis (set by hand with `redis-cli SET`):

| Key | Default | Meaning |
|---|---|---|
| `tariffNight` | 6.66 | off-peak rate, p/kWh (23:00–06:00) |
| `tariffDay` | 32.52 | peak rate, p/kWh |
| `mpg` | 40 | petrol car used for comparison |
| `ppmWindowDays` | 42 | trailing window (days) for the measured cost per mile |

## Redis data

Plain keys (latest values): `batteryPercentage`, `currentRangeMiles`, `100Range`, `milesPerKwh` (what the car predicts), `measuredMilesPerKwh` (what it actually did), `totalMiles`, `averageMileCost`, `accumCostElec`, `accumCostPetrol`, `pricePerLitre`, `lastChargeStart` (dedupe checkpoint, epoch ms).

Streams (time series for Grafana):

| Stream | Written | Fields |
|---|---|---|
| `battery_stats` | every run | `battery_pct`, `efficiency` |
| `charge_stats` | once per charge, timestamped at charge start | `miles_added`, `cost`, `efficiency`, `hours`, `kWh_added`, `kWh_bought`, `%_added`, `offpeak_pct`, `rate_p`, `home` |
| `mile_stats` | every run | `mile_delta`, `ppm`, `miles/kwh` (predicted), `measured_miles/kwh` (when enough data) |

## How costs are worked out

- **Predicted efficiency** (mi/kWh) is the car's 100%-range estimate divided by 52 kWh. The car never shows this directly, only predicted range, so it's backed out from range ÷ battery %. It moves with driving style and season, which is why it's tracked, but it's a prediction so no money is based on it.
- **Charge cost** is duration × 7.45 kW (Hypervolt) × rate, with the rate blended by how much of the charge fell in the off-peak window. Each charge's cost is added to the electric total as it happens.
- **Public charges** (energy added faster than the Hypervolt can deliver) have a known energy but unknown price, so they're recorded with `home=0` and cost 0, and an alert is sent. Add the real cost by hand: `redis-cli INCRBYFLOAT accumCostElec <pounds>`.
- **Petrol total** adds miles driven (odometer delta each run) × (price per litre × 4.546 ÷ mpg). Savings = petrol − electric.
- **Measured cost per mile** (`averageMileCost`) is home charge cost over the last `ppmWindowDays` divided by miles driven in the same window, corrected for the battery holding a different amount at the start and end of the window. `measuredMilesPerKwh` comes from the same sums. Both need at least 100 miles in the window, otherwise the previous value is kept.
- An odometer that goes backwards is ignored. A missing odometer baseline (first run, or Redis lost its data) records the current reading and costs nothing, rather than counting the car's whole mileage as driven.

## Grafana

`grafana/renault5-dashboard.json` is the dashboard (Grafana v2 schema, Redis datasource). Import it via Dashboards → New → Import, or `PUT` it to the dashboard API to overwrite the existing one; it keeps the same UID. The cost-per-mile comparison needs `mpg` set in Redis.

## Build and release

- Push to `main` → image tagged `edge` and `sha-…`
- Publish a GitHub release `vX.Y.Z` → `vX.Y.Z`, `vX.Y`, `vX`, `latest`
- Pull requests are built but not pushed
- `test.sh` builds and pushes `:test` from a local checkout

Run locally:

```sh
pip install -r requirements.txt
python renault.py
python fuel_price.py
```
