#!/bin/python3
"""Fetch the E10 price at one station from the gov.uk Fuel Finder API and store it in Redis."""

import logging
import os
import sys

import redis
import requests
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=os.getenv('LOG_LEVEL', 'INFO'),
    format=os.getenv('LOG_FORMAT', '%(levelname)-7s %(message)s'),
)
log = logging.getLogger('fuel_price')

BASE_URL = "https://www.fuel-finder.service.gov.uk"
STATION_ID = "9dfd7ba2235eb23f04d99548373ad1254c64b2abb45e366085281d1d1dd4e999"  # Wolverton Tesco
FUEL_TYPE = "E10"
MAX_BATCHES = 24
TIMEOUT = 30

CLIENT_ID = os.getenv('ONEGOV_CLIENT_ID')
CLIENT_SECRET = os.getenv('ONEGOV_CLIENT_SECRET')
REDIS_HOST = os.getenv('REDIS_HOST', 'localhost')


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


def get_access_token():
    response = requests.post(
        f"{BASE_URL}/api/v1/oauth/generate_access_token",
        json={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "client_credentials",
            "scope": "fuelfinder.read",
        },
        headers={"accept": "application/json"},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    return response.json()['data']['access_token']


def find_price(access_token):
    """Page through station batches until our station turns up. Returns £/litre, or None."""
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    for batch_no in range(1, MAX_BATCHES + 1):
        response = requests.get(
            f"{BASE_URL}/api/v1/pfs/fuel-prices",
            params={"batch-number": batch_no},
            headers=headers,
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        for station in response.json():
            if station['node_id'] != STATION_ID:
                continue
            for fuel_price in station['fuel_prices']:
                if fuel_price['fuel_type'] == FUEL_TYPE:
                    log.debug("station found in batch %d", batch_no)
                    return round(float(fuel_price['price']) / 100, 3)
            log.warning("station found in batch %d but it has no %s price", batch_no, FUEL_TYPE)
            return None
    log.warning("station not found in %d batches", MAX_BATCHES)
    return None


def main():
    price = find_price(get_access_token())
    if price is None:
        notify("Couldn't update fuel price: station or price not found")
        sys.exit(1)

    r = redis.Redis(host=REDIS_HOST, port=6379, db=0)
    r.set('pricePerLitre', price)
    r.close()
    log.info("%s at Wolverton Tesco: £%.3f/L", FUEL_TYPE, price)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log.exception("fuel price update failed")
        notify(f"fuel_price.py failed: {e!r}")
        sys.exit(1)
