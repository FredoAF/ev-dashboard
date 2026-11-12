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

# EDF off-peak window (local time). Rates (p/kWh) live in Redis: tariffNight / tariffDay
NIGHT_START = time(23, 0)
NIGHT_END = time(6, 0)
TARIFF_TZ = ZoneInfo(os.getenv('TARIFF_TZ', 'Europe/London'))
DEFAULT_NIGHT_RATE = 6.66
DEFAULT_DAY_RATE = 32.52

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

async def main():
    # Load state from Redis
    price_per_litre = get_redis_float('pricePerLitre', 1.50)
    mpg = get_redis_int('mpg', 40)
    night_rate = get_redis_float('tariffNight', DEFAULT_NIGHT_RATE) / 100  # pence -> pounds
    day_rate = get_redis_float('tariffDay', DEFAULT_DAY_RATE) / 100
    last_charge_ms = get_redis_int(
        'lastChargeStart', int((datetime.now().timestamp() - 3 * 3600) * 1000)
    )
    prev_total_miles = get_redis_int('totalMiles', 0)
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
        cost_per_mile_array = [current_average_mile_cost]
        try:
            charge_history = await vehicle.get_charges(datetime.now() - timedelta(days=1), datetime.now())
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
                pct_added = charge["chargeEndBatteryLevel"] - charge["chargeStartBatteryLevel"]
                energy_recovered = charge["chargeEnergyRecovered"] # kWh added to battery
                miles_added = round(energy_recovered * miles_per_kwh)

                # Calculate cost based on charger power and time, split across tariff windows
                charge_hours = charge["chargeDuration"] / 60
                kwh_bought = charge_hours * CHARGER_POWER_KW
                night_frac = night_fraction(start, start + timedelta(hours=charge_hours))
                rate = night_frac * night_rate + (1 - night_frac) * day_rate
                charge_cost = kwh_bought * rate

                # Efficiency of the charger (how much energy hit the battery vs what was pulled from wall)
                efficiency = (energy_recovered / kwh_bought * 100) if kwh_bought > 0 else 0
                cost_per_mile = (charge_cost / miles_added) if miles_added > 0 else 0

                # Anything not fully off-peak is worth flagging
                level = logging.INFO if night_frac >= 0.999 else logging.WARNING
                log.log(level, "charge %s | +%d mi (%d%%) | %.1f kWh @ %.2fp | £%.2f | off-peak %.0f%% | eff %.0f%%",
                        start.astimezone(TARIFF_TZ).strftime("%Y-%m-%d %H:%M"), miles_added, pct_added,
                        kwh_bought, rate * 100, charge_cost, night_frac * 100, efficiency)
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
                }
                # Timestamp the entry at the charge start so Grafana plots it at the real time;
                # fall back to "now" if the stream already has a later entry.
                try:
                    r.xadd("charge_stats", fields, id=f"{start_ms}-0")
                except redis.ResponseError:
                    r.xadd("charge_stats", fields)
                r.set('lastChargeStart', start_ms)

                if cost_per_mile > 0:
                    cost_per_mile_array.append(cost_per_mile)

        # 3. Accumulative Tracking
        current_average_mile_cost = sum(cost_per_mile_array) / len(cost_per_mile_array)
        r.set('averageMileCost', current_average_mile_cost)

        # Mileage
        delta = 0
        if total_miles < prev_total_miles:
            # Odometers don't go backwards: treat as a bad reading and keep the last good value,
            # otherwise the next run would count the whole odometer as distance driven.
            log.warning("ignoring odometer reading %d mi (last good: %d mi)", total_miles, prev_total_miles)
            notify(f"Odometer went backwards ({prev_total_miles} -> {total_miles} mi), ignored")
            total_miles = prev_total_miles
        elif total_miles > prev_total_miles:
            delta = total_miles - prev_total_miles
            accum_cost_elec += delta * current_average_mile_cost
            accum_cost_petrol += delta * petrol_ppm
            r.set('accumCostElec', accum_cost_elec)
            r.set('accumCostPetrol', accum_cost_petrol)

        log.info("mileage %s mi (+%d) | elec %.1fp/mi vs petrol %.1fp/mi | saved £%.2f",
                 f"{total_miles:,}", delta, current_average_mile_cost * 100, petrol_ppm * 100,
                 accum_cost_petrol - accum_cost_elec)
        r.set('totalMiles', total_miles)
        r.xadd("mile_stats", {
            'mile_delta': delta,
            'ppm': current_average_mile_cost,
            'miles/kwh': miles_per_kwh
        })
        r.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        log.exception("run failed")
        notify(f"renault.py failed: {e!r}")
        sys.exit(1)
