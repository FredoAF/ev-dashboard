#!/bin/python3
"""Pull battery, charge and mileage data from the Renault API and store it in Redis."""

import asyncio
import logging
import os
import sys
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import aiohttp
import redis
import requests
from dotenv import load_dotenv
from renault_api.renault_client import RenaultClient

# --- Configuration & Constants ---
load_dotenv()

logging.basicConfig(
    level=os.getenv('LOG_LEVEL', 'INFO'),
    format=os.getenv('LOG_FORMAT', '%(levelname)-7s %(message)s'),
)
logging.getLogger('renault_api').setLevel(logging.WARNING)
log = logging.getLogger('renault')

KM_TO_MILES = 0.6214
LITRES_PER_GALLON = 4.546
BATTERY_SIZE_KWH = 52.0  # Renault 5
CHARGER_POWER_KW = 7.45  # Hypervolt
# A charge that put energy into the battery faster than this can't have come from the
# Hypervolt (public rapid, someone else's wallbox), so its cost is unknown.
HOME_MAX_KW = CHARGER_POWER_KW * 1.15

# EDF off-peak window (local time). Rates (p/kWh) live in Redis: tariffNight / tariffDay
NIGHT_START = time(23, 0)
NIGHT_END = time(6, 0)
TARIFF_TZ = ZoneInfo(os.getenv('TARIFF_TZ', 'Europe/London'))
DEFAULT_NIGHT_RATE = 6.66
DEFAULT_DAY_RATE = 32.52

# Measured cost per mile is worked out over a trailing window of real charges and real
# miles. Needs enough miles in the window to be meaningful, else the last value is kept.
DEFAULT_PPM_WINDOW_DAYS = 42
MIN_WINDOW_MILES = 100

REDIS_HOST = os.getenv('REDIS_HOST', 'localhost')
ACCOUNT_ID = os.getenv("RENAULT_ACCOUNT_ID")
USERNAME = os.getenv("RENAULT_USERNAME")
PASSWORD = os.getenv("RENAULT_PASSWORD")
VIN = os.getenv("RENAULT_VIN")

# --- Redis Setup ---
r = redis.Redis(host=REDIS_HOST, port=6379, db=0, decode_responses=True)

def get_redis_float(key, default=0.0):
    val = r.get(key)
    return float(val) if val else default

def get_redis_int(key, default=0):
    val = r.get(key)
    return int(val) if val else default

def notify(msg):
    try:
        requests.post(
            "https://ntfy.sh/farren-renault",
            data=msg.encode(encoding="utf-8"),
            headers={"Title": "R5", "Tags": "bug"},
            timeout=10,
        )
    except requests.RequestException as e:
        log.warning("failed to send notification: %s", e)

# --- Logic Helpers ---
def calculate_petrol_ppm(mpg, price_per_litre):
    """Calculates price per mile for a petrol car."""
    price_per_gallon = price_per_litre * LITRES_PER_GALLON
    return price_per_gallon / mpg

def parse_time(dt_str):
    """Parse a Renault API timestamp (UTC, e.g. 2025-01-01T23:00:00Z) to an aware datetime."""
    return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))

def night_fraction(start, end):
    """Fraction of [start, end] that falls inside the off-peak window."""
    total = (end - start).total_seconds()
    if total <= 0:
        return 1.0
    local_start = start.astimezone(TARIFF_TZ)
    local_end = end.astimezone(TARIFF_TZ)
    overlap = 0.0
    # Each window opens at NIGHT_START on `day` and closes at NIGHT_END the next day
    day = local_start.date() - timedelta(days=1)
    while day <= local_end.date():
        win_start = datetime.combine(day, NIGHT_START, TARIFF_TZ)
        win_end = datetime.combine(day + timedelta(days=1), NIGHT_END, TARIFF_TZ)
        overlap += max(0.0, (min(end, win_end) - max(start, win_start)).total_seconds())
        day += timedelta(days=1)
    return overlap / total

def default_last_charge_ms():
    """Checkpoint to use when 'lastChargeStart' has never been set.

    Charges starting after the newest charge_stats entry haven't been recorded yet.
    Must not be relative to "now": a charge still running at one run would then fall
    behind the cutoff by the next run and never be recorded.
    """
    newest = r.xrevrange("charge_stats", count=1)
    if newest:
        return int(newest[0][0].split("-")[0])
    return int((datetime.now().timestamp() - 1 * 86400) * 1000)

def measured_cost_per_mile(window_days, extra_miles=0):
    """Real £/mile and mi/kWh over the trailing window, from what was paid and what was driven.

    Home charge cost in the window is divided by miles driven in the window. The battery
    held a different amount at the start and end of the window, so that difference is
    converted to kWh and priced at the window's average cost per kWh before dividing.
    Returns (ppm, miles_per_kwh), or (None, None) if there isn't enough data yet.
    `extra_miles` is this run's mileage delta, not yet in the mile_stats stream.
    """
    since = f"{int((datetime.now().timestamp() - window_days * 86400) * 1000)}-0"
    charges, seen = [], set()
    for _, f in r.xrange("charge_stats", min=since):
        if f.get('home', '1') != '1':
            continue
        # Until Nov 2026 the dedupe checkpoint didn't work and every run re-recorded the
        # same charges, so identical entries are counted once.
        key = (f.get('cost'), f.get('kWh_added'), f.get('hours'), f.get('%_added'))
        if key in seen:
            continue
        seen.add(key)
        charges.append(f)
    cost = sum(float(f['cost']) for f in charges)
    kwh_added = sum(float(f.get('kWh_added', 0)) for f in charges)
    miles = sum(int(float(f['mile_delta'])) for _, f in r.xrange("mile_stats", min=since)) + extra_miles
    battery = r.xrange("battery_stats", min=since)
    if miles < MIN_WINDOW_MILES or kwh_added <= 0 or not battery:
        return None, None
    soc_drop_kwh = (float(battery[0][1]['battery_pct']) - float(battery[-1][1]['battery_pct'])) / 100 * BATTERY_SIZE_KWH
    kwh_used = kwh_added + soc_drop_kwh
    if kwh_used <= 0:
        return None, None
    cost_used = cost * kwh_used / kwh_added
    return cost_used / miles, miles / kwh_used

async def main():
    # Load state from Redis
    price_per_litre = get_redis_float('pricePerLitre', 1.50)
    mpg = get_redis_float('mpg', 40)
    night_rate = get_redis_float('tariffNight', DEFAULT_NIGHT_RATE) / 100  # pence -> pounds
    day_rate = get_redis_float('tariffDay', DEFAULT_DAY_RATE) / 100
    ppm_window_days = get_redis_int('ppmWindowDays', DEFAULT_PPM_WINDOW_DAYS)
    last_charge_ms = get_redis_int('lastChargeStart', default_last_charge_ms())
    prev_total_miles_raw = r.get('totalMiles')
    prev_total_miles = int(prev_total_miles_raw) if prev_total_miles_raw else None
    current_average_mile_cost = get_redis_float('averageMileCost', 0.055)
    accum_cost_elec = get_redis_float('accumCostElec', 0.0)
    accum_cost_petrol = get_redis_float('accumCostPetrol', 0.0)

    async with aiohttp.ClientSession() as websession:
        client = RenaultClient(websession=websession, locale="en_GB")
        await client.session.login(USERNAME, PASSWORD)

        account = await client.get_api_account(ACCOUNT_ID)
        vehicle = await account.get_api_vehicle(VIN)

        cockpit = await vehicle.get_cockpit()
        battery = await vehicle.get_battery_status()

        # Bail out before writing anything if the car didn't report what we need
        missing = [name for name, val in (
            ('totalMileage', cockpit.totalMileage),
            ('batteryLevel', battery.batteryLevel),
            ('batteryAutonomy', battery.batteryAutonomy),
        ) if val is None]
        if missing:
            raise ValueError(f"Car returned no value for: {', '.join(missing)}")

        total_miles = round(cockpit.totalMileage * KM_TO_MILES)
        petrol_ppm = calculate_petrol_ppm(mpg, price_per_litre)

        # 1. Battery & Efficiency
        # The car only shows predicted range; range / battery % / 52 kWh backs out the
        # mi/kWh it is currently assuming. Display only: it's a prediction, so money is
        # never based on it (see measured_cost_per_mile).
        battery_pct = battery.batteryLevel
        range_miles = battery.batteryAutonomy * KM_TO_MILES

        # Avoid division by zero if battery is 0%
        full_range_est = (range_miles / battery_pct * 100) if battery_pct > 0 else 0
        miles_per_kwh = full_range_est / BATTERY_SIZE_KWH

        log.info("battery %d%% | range %.1f mi (100%%: %.1f mi) | %.2f mi/kWh",
                 battery_pct, range_miles, full_range_est, miles_per_kwh)
        r.xadd("battery_stats", {
            'battery_pct': battery_pct,
            'efficiency': miles_per_kwh
        })
        r.mset({
            '100Range': round(full_range_est, 1),
            'currentRangeMiles': round(range_miles, 1),
            'milesPerKwh': round(miles_per_kwh, 2),
            'batteryPercentage': battery_pct
        })

        # 2. Recent Charging History
        # The API only filters by *day*, so every run sees all of today's (and
        # yesterday's) charges. 'lastChargeStart' tracks what has already been recorded.
        # Look back to the last recorded charge so an outage longer than a day doesn't
        # lose charges for good.
        lookback_from = min(datetime.now() - timedelta(days=1),
                            datetime.fromtimestamp(last_charge_ms / 1000))
        try:
            charge_history = await vehicle.get_charges(lookback_from, datetime.now())
        except Exception as e:
            log.warning("could not fetch charge history: %s", e)
        else:
            charges_list = charge_history.raw_data.get("charges", [])
            new_charges = []
            for charge in charges_list:
                if not charge.get("chargeStartDate") or not charge.get("chargeEndDate"):
                    continue  # still in progress, pick it up next run
                start = parse_time(charge["chargeStartDate"])
                start_ms = int(start.timestamp() * 1000)
                if start_ms > last_charge_ms:
                    new_charges.append((start_ms, start, charge))
            new_charges.sort(key=lambda c: c[0])

            if not new_charges:
                log.info("charges: none new")

            for start_ms, start, charge in new_charges:
                end = parse_time(charge["chargeEndDate"])
                pct_added = charge["chargeEndBatteryLevel"] - charge["chargeStartBatteryLevel"]
                energy_recovered = charge["chargeEnergyRecovered"] # kWh added to battery
                miles_added = round(energy_recovered * miles_per_kwh, 1)  # display only, uses the car's prediction
                charge_hours = charge["chargeDuration"] / 60
                when = start.astimezone(TARIFF_TZ).strftime("%Y-%m-%d %H:%M")

                # Sanity checks on what the car reported
                wall_hours = (end - start).total_seconds() / 3600
                if abs(wall_hours - charge_hours) > 10 / 60:
                    log.warning("charge %s | duration %.2fh but start->end is %.2fh (paused, or units wrong?)",
                                when, charge_hours, wall_hours)
                kwh_from_pct = pct_added / 100 * BATTERY_SIZE_KWH
                if kwh_from_pct > 0 and abs(energy_recovered - kwh_from_pct) / kwh_from_pct > 0.3:
                    log.warning("charge %s | car reports %.1f kWh added but %d%% is ~%.1f kWh",
                                when, energy_recovered, pct_added, kwh_from_pct)

                implied_kw = (energy_recovered / charge_hours) if charge_hours > 0 else 0
                is_home = implied_kw <= HOME_MAX_KW

                if is_home:
                    # Cost = what the Hypervolt pulled from the wall (power x time) at the
                    # tariff rate, blended by how much of the charge fell in the off-peak window
                    kwh_bought = charge_hours * CHARGER_POWER_KW
                    night_frac = night_fraction(start, start + timedelta(hours=charge_hours))
                    rate = night_frac * night_rate + (1 - night_frac) * day_rate
                    charge_cost = kwh_bought * rate
                    # Efficiency of the charger (how much energy hit the battery vs what was pulled from wall)
                    efficiency = (energy_recovered / kwh_bought * 100) if kwh_bought > 0 else 0
                    accum_cost_elec += charge_cost

                    # Anything not fully off-peak is worth flagging
                    level = logging.INFO if night_frac >= 0.999 else logging.WARNING
                    log.log(level, "charge %s | +%.1f mi (%d%%) | %.1f kWh @ %.2fp | £%.2f | off-peak %.0f%% | eff %.0f%%",
                            when, miles_added, pct_added, kwh_bought, rate * 100, charge_cost,
                            night_frac * 100, efficiency)
                else:
                    # Not the home charger: we know the energy but not the price, so it is
                    # recorded uncosted and flagged for a manual top-up of accumCostElec.
                    kwh_bought = charge_cost = efficiency = night_frac = rate = 0
                    log.warning("charge %s | +%.1f mi (%d%%) | %.1f kWh at ~%.0f kW looks like a public charger, not costed",
                                when, miles_added, pct_added, energy_recovered, implied_kw)
                    notify(f"Charge at {when} ({energy_recovered:.1f} kWh at ~{implied_kw:.0f} kW) looks like a "
                           f"public charger. Not costed; add it with: INCRBYFLOAT accumCostElec <pounds>")

                fields = {
                    'miles_added': miles_added,
                    'cost': charge_cost,
                    'efficiency': efficiency,
                    'hours': charge_hours,
                    'kWh_added': energy_recovered,
                    'kWh_bought': kwh_bought,
                    '%_added': pct_added,
                    'offpeak_pct': round(night_frac * 100, 1),
                    'rate_p': round(rate * 100, 2),
                    'home': int(is_home),
                }
                # Timestamp the entry at the charge start so Grafana plots it at the real time;
                # fall back to "now" if the stream already has a later entry.
                try:
                    r.xadd("charge_stats", fields, id=f"{start_ms}-0")
                except redis.ResponseError:
                    r.xadd("charge_stats", fields)
                r.set('lastChargeStart', start_ms)
            if new_charges:
                r.set('accumCostElec', accum_cost_elec)

        # 3. Mileage & petrol comparison
        delta = 0
        if prev_total_miles is None:
            # First run (or Redis lost its data): no baseline, so nothing to cost yet,
            # otherwise the car's whole odometer would be counted as miles driven.
            log.warning("no previous odometer reading, starting from %d mi", total_miles)
        elif total_miles < prev_total_miles:
            # Odometers don't go backwards: treat as a bad reading and keep the last good value,
            # otherwise the next run would count the whole odometer as distance driven.
            log.warning("ignoring odometer reading %d mi (last good: %d mi)", total_miles, prev_total_miles)
            notify(f"Odometer went backwards ({prev_total_miles} -> {total_miles} mi), ignored")
            total_miles = prev_total_miles
        elif total_miles > prev_total_miles:
            delta = total_miles - prev_total_miles
            if delta > 500:
                log.warning("%d mi since last run, check the odometer reading", delta)
            # Petrol: what those miles would have cost at today's pump price
            accum_cost_petrol += delta * petrol_ppm
            r.set('accumCostPetrol', accum_cost_petrol)

        # Electric cost per mile is measured, not predicted: real home charge cost over
        # real miles driven in the trailing window. Falls back to the last value until
        # there is enough data.
        measured_ppm, measured_mi_per_kwh = measured_cost_per_mile(ppm_window_days, delta)
        if measured_ppm is not None:
            current_average_mile_cost = measured_ppm
            r.set('averageMileCost', current_average_mile_cost)
            r.set('measuredMilesPerKwh', round(measured_mi_per_kwh, 2))
        else:
            log.info("not enough data in the last %d days for a measured cost per mile", ppm_window_days)

        log.info("mileage %s mi (+%d) | elec %.1fp/mi vs petrol %.1fp/mi | measured %s mi/kWh | saved £%.2f",
                 f"{total_miles:,}", delta, current_average_mile_cost * 100, petrol_ppm * 100,
                 f"{measured_mi_per_kwh:.2f}" if measured_mi_per_kwh is not None else "n/a",
                 accum_cost_petrol - accum_cost_elec)
        r.set('totalMiles', total_miles)
        mile_fields = {
            'mile_delta': delta,
            'ppm': current_average_mile_cost,
            'miles/kwh': miles_per_kwh,
        }
        if measured_mi_per_kwh is not None:
            mile_fields['measured_miles/kwh'] = measured_mi_per_kwh
        r.xadd("mile_stats", mile_fields)
        r.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        log.exception("run failed")
        notify(f"renault.py failed: {e!r}")
        sys.exit(1)
