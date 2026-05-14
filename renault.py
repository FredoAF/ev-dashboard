#!/bin/python3

import asyncio
import os
from datetime import datetime, timedelta

import aiohttp
import redis
from dotenv import load_dotenv
from renault_api.renault_client import RenaultClient

# --- Configuration & Constants ---
load_dotenv()

KM_TO_MILES = 0.6214
LITRES_PER_GALLON = 4.546
BATTERY_SIZE_KWH = 52.0  # Renault 5
CHARGER_POWER_KW = 7.45  # Hypervolt 

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

# --- Logic Helpers ---
def calculate_petrol_ppm(mpg, price_per_litre):
    """Calculates price per mile for a petrol car."""
    price_per_gallon = price_per_litre * LITRES_PER_GALLON
    return price_per_gallon / mpg

def convert_time(dt_str):
    dt_obj = datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%SZ")
    ts_ms = int(dt_obj.timestamp() * 1000)
    return ts_ms

async def main():
    # Load state from Redis
    try:
        price_per_litre = get_redis_float('pricePerLitre', 1.50)
        mpg = get_redis_int('mpg', 40)
        tariff = get_redis_float('tariff', 20) / 100  # Convert pence to pounds
        prev_total_miles = get_redis_int('totalMiles', 0)
        current_average_mile_cost = get_redis_float('averageMileCost', 0.055)
        accum_cost_elec = get_redis_float('accumCostElec', 0.0)
        accum_cost_petrol = get_redis_float('accumCostPetrol', 0.0)
    except Exception as e:
        print(f"Error loading Redis data: {e}")
        return

    async with aiohttp.ClientSession() as websession:
        client = RenaultClient(websession=websession, locale="en_GB")
        await client.session.login(USERNAME, PASSWORD)
        
        account = await client.get_api_account(ACCOUNT_ID)
        vehicle = await account.get_api_vehicle(VIN)

        # 1. Mileage & Savings Comparison
        
        cockpit = await vehicle.get_cockpit()
        total_miles = round(cockpit.totalMileage * KM_TO_MILES)
        petrol_ppm = calculate_petrol_ppm(mpg, price_per_litre)
        # total_petrol_equiv_cost = petrol_ppm * total_miles
        # total_elec_equiv_cost = current_average_mile_cost * total_miles
        # total_saving = total_petrol_equiv_cost - total_elec_equiv_cost

        

        # Sync to Redis
        
        # r.set('totalPetrolCost', total_petrol_equiv_cost)
        # r.set('totalElectricCost', total_elec_equiv_cost)
        # r.set('totalSaving', total_saving)

        # 2. Battery & Efficiency
        print("\n--- Battery Status ---")
        battery = await vehicle.get_battery_status()
        battery_pct = battery.batteryLevel
        range_miles = battery.batteryAutonomy * KM_TO_MILES
        
        # Avoid division by zero if battery is 0%
        full_range_est = (range_miles / battery_pct * 100) if battery_pct > 0 else 0
        miles_per_kwh = full_range_est / BATTERY_SIZE_KWH

        print(f"Level: {battery_pct}% | Est. Range: {range_miles:.1f} mi")
        print(f"Efficiency: {miles_per_kwh:.2f} mi/kWh")
        
        r.mset({
            '100Range': round(full_range_est, 1),
            'currentRangeMiles': round(range_miles, 1),
            'milesPerKwh': round(miles_per_kwh, 2),
            'batteryPercentage': battery_pct
        })

        # 3. Recent Charging History (Last 7 Days)
        cost_per_mile_array = []
        cost_per_mile_array.append(current_average_mile_cost)
        try:
            charge_history = await vehicle.get_charges(datetime.now() - timedelta(hours=3), datetime.now())
        except:
            print(f"No charge history found for period")
        else:
            print("\n--- Recent Charges ---")

            # Accessing the raw data list safely
            charges_list = charge_history.raw_data.get("charges", [])
            
            for charge in charges_list:
                pct_added = charge["chargeEndBatteryLevel"] - charge["chargeStartBatteryLevel"]
                energy_recovered = charge["chargeEnergyRecovered"] # kWh added to battery
                miles_added = round(energy_recovered * miles_per_kwh)
                
                # Calculate cost based on charger power and time
                charge_hours = charge["chargeDuration"] / 60
                kwh_bought = charge_hours * CHARGER_POWER_KW
                charge_cost = kwh_bought * tariff
                
                # Efficiency of the charger (how much energy hit the battery vs what was pulled from wall)
                efficiency = (energy_recovered / kwh_bought * 100) if kwh_bought > 0 else 0
                cost_per_mile = (charge_cost / miles_added) if miles_added > 0 else 0
                print(f"Date: {charge['chargeStartDate']} | Added: {miles_added}mi | Cost: £{charge_cost:.2f} | Eff: {efficiency:.1f}%")
                r.xadd("charge_stats", {
                    'miles_added': miles_added,
                    'cost': charge_cost,
                    'efficiency': efficiency,
                    'hours': charge_hours,
                    'kWh_added': energy_recovered,
                    'kWh_bought': kwh_bought,
                    '%_added': pct_added
                })
                
                if cost_per_mile > 0:
                    cost_per_mile_array.append(cost_per_mile)

        # 4. Accumulative Tracking
        if cost_per_mile_array:
            avg_mile_cost = sum(cost_per_mile_array) / len(cost_per_mile_array)
            print(f"\nAverage Cost per Mile: £{avg_mile_cost:.3f}")
            r.set('averageMileCost', avg_mile_cost)
            current_average_mile_cost = avg_mile_cost

        # Mileage
        delta = 0
        print("--- Mileage Information ---")
        if total_miles > prev_total_miles:
            diff = total_miles - prev_total_miles
            delta = diff
            new_elec_cost = diff * current_average_mile_cost
            new_petrol_cost = diff * petrol_ppm
            
            r.set('accumCostElec', accum_cost_elec + new_elec_cost)
            r.set('accumCostPetrol', accum_cost_petrol + new_petrol_cost)
            print(f"Updated Accumulative Costs. Distance since last run: {diff} mi")
        print(f"Total Mileage: {total_miles} mi")
        print(f"Electirc PPM: £{current_average_mile_cost:.2f} | Est. Savings: £{get_redis_float('accumCostPetrol') - get_redis_float('accumCostElec') }")
        r.set('totalMiles', total_miles)
        r.xadd("mile_stats", {
            'mile_delta': delta,
            'ppm': current_average_mile_cost,
            'miles/kwh': miles_per_kwh
        })
        r.close()

if __name__ == "__main__":
    asyncio.run(main())