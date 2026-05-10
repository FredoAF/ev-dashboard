#!/bin/python3

import aiohttp
import asyncio
import json
from datetime import datetime
import redis
import os
from renault_api.renault_client import RenaultClient

REDIS = os.environ.get('REDIS_HOST')
ACCOUNT_ID = os.getenv("RENAULT_ACCOUNT_ID")
USERNAME = os.getenv("RENAULT_USERNAME")
PASSWORD = os.getenv("RENAULT_PASSWORD")
VIN = os.getenv("RENAULT_VIN")

r = redis.Redis(host=REDIS, port=6379, db=0)

# r.set('pricePerLitre', 1.56)
# r.set('mpg', 40)
kmToMiles = 0.6214
pricePerLitre = 1.56
mpg = 40


def pricePerMile(mpg, pricePerLitre):
    pricePerGallon = pricePerLitre * 4.546
    return pricePerGallon / mpg


async def main():
    async with aiohttp.ClientSession() as websession:
        # Login and setup
        client = RenaultClient(websession=websession, locale="en_GB")
        await client.session.login(USERNAME, PASSWORD)

        account = await client.get_api_account(ACCOUNT_ID)

        vehicle = await account.get_api_vehicle(VIN)

        print(f"Mileage information:")
        cockpit = await vehicle.get_cockpit()
        cockpitJson = vars(cockpit).copy()
        totalKM = cockpitJson["totalMileage"]
        totalMiles = round(totalKM * kmToMiles)
        print(f"  Total milage driven: {totalMiles}")
        milePrice = pricePerMile(mpg, pricePerLitre)
        totalPetrolCost = milePrice * totalMiles
        totalElectricCost = 0.055 * totalMiles
        totalSaving = totalPetrolCost - totalElectricCost
        print(f"  Price per mile for petrol: £{round(milePrice, 2)}")
        print(f"  Price of {totalMiles} miles in petrol: £{round(totalPetrolCost, 2)}")
        print(f"  Price of {totalMiles} miles in electric: £{round(totalElectricCost,2)}")
        print(f"  Saving of {totalMiles} miles driving electric: £{round(totalSaving, 2)}")
        r.set('totalMiles', totalMiles)
        r.set('totalPetrolCost', totalPetrolCost)
        r.set('totalElectricCost', totalElectricCost)
        r.set('totalSaving', totalSaving)

        print(f"Battery status information:")
        battery = await vehicle.get_battery_status()
        batteryJson = vars(battery).copy()
        batteryPercentage = batteryJson["batteryLevel"]
        currentRangeKM = batteryJson["batteryAutonomy"]
        currentRangeMiles = currentRangeKM * kmToMiles
        totalRange = (currentRangeMiles / batteryPercentage) * 100
        milesPerKwh = totalRange / 52  # 52kwh battery at 100%
        print(f"  100% Range: {round(totalRange)}")
        print(f"  {batteryPercentage}% Range: {round(currentRangeMiles)}")
        print(f"  mi/kWh: {round(milesPerKwh)}")
        r.set('100Range', totalRange)
        r.set('currentRangeMiles', currentRangeMiles)
        r.set('milesPerKwh', milesPerKwh)


        charges = await vehicle.get_charges(datetime(2026, 5, 1), datetime.now())
        chargesJson = vars(charges).copy()
        for charge in chargesJson["raw_data"]["charges"]:
            print("Charge:")
            totalPercentAdded = (
                charge["chargeEndBatteryLevel"] - charge["chargeStartBatteryLevel"]
            )
            print(f"  Percentage charged: {totalPercentAdded}%")
            totalMilesAdded = round(charge["chargeEnergyRecovered"] * milesPerKwh)
            print(f"  Miles Added: {totalMilesAdded}")
            chargeHours = charge["chargeDuration"] / 60
            totalKwhBought = chargeHours * 7.45  # kwh that hypervolt uses
            totalKwhCost = totalKwhBought * 0.209  # current tarrif
            print(f"  Charged hours: {round(chargeHours, 2)}")
            print(f"  kWh Bought: {round(totalKwhBought, 2)}")
            print(f"  Total cost of charge: £{round(totalKwhCost, 2)}")
            costPerMile = totalKwhCost / totalMilesAdded
            print(f"  Price per mile: £{round(costPerMile, 3)}")


if __name__ == "__main__":
    asyncio.run(main())
