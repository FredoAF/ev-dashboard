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
| `mpg` | 40 | petrol car used for comparison (whole number) |

## Redis data

Plain keys (latest values): `batteryPercentage`, `currentRangeMiles`, `100Range`, `milesPerKwh`, `totalMiles`, `averageMileCost`, `accumCostElec`, `accumCostPetrol`, `pricePerLitre`, `lastChargeStart` (dedupe checkpoint, epoch ms).

Streams (time series for Grafana):

| Stream | Written | Fields |
|---|---|---|
| `battery_stats` | every run | `battery_pct`, `efficiency` |
| `charge_stats` | once per charge, timestamped at charge start | `miles_added`, `cost`, `efficiency`, `hours`, `kWh_added`, `kWh_bought`, `%_added`, `offpeak_pct`, `rate_p` |
| `mile_stats` | every run | `mile_delta`, `ppm`, `miles/kwh` |

## How costs are worked out

- **Efficiency** (mi/kWh) is the car's 100%-range estimate divided by 52 kWh.
- **Charge cost** is duration × 7.45 kW (Hypervolt) × rate, with the rate blended by how much of the charge fell in the off-peak window.
- **Electric total** adds miles driven × average cost per mile; **petrol total** adds miles driven × (price per litre × 4.546 ÷ mpg). Savings = petrol − electric.

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
