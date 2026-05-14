import requests
import redis
import os

# --- CONFIGURATION ---
REDIS = os.environ.get('REDIS_HOST')
API_KEY = os.environ.get('OCTOPUS_API_KEY')
ACCOUNT_ID = os.environ.get('OCTOPUS_ACCOUNT_ID')

r = redis.Redis(host=REDIS, port=6379, db=0)

# Base URL for the Octopus API
BASE_URL = "https://api.octopus.energy/v1"

def notify(msg):
    requests.post(
        "https://ntfy.sh/farren-renault",
        data=msg.encode(encoding="utf-8"),
        headers={"Title": "R5", "Tags": "bug"},
    )

def get_current_unit_rate():
    # 1. Get Account Details to find the active tariff
    acc_url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/"
    response = requests.get(acc_url, auth=(API_KEY, ''))
    
    if response.status_code != 200:
        print(f"Error fetching account: {response.status_code}")
        return

    acc_data = response.json()
    meter_point = acc_data['properties'][0]['electricity_meter_points'][0]
    
    tariff_code = meter_point['agreements'][0]['tariff_code']
    
    # The product code is the middle chunk of the tariff code
    # e.g., 'E-1R-AGILE-24-04-03-L' -> 'AGILE-24-04-03'
    # Logic: Split by '-', remove first two (E-1R) and last one (Region L)
    parts = tariff_code.split('-')
    product_code = "-".join(parts[2:-1])
    
    print(f"--- Discovery ---")
    print(f"Active Tariff:  {tariff_code}")
    print(f"Product Code:   {product_code}")
    print(f"-----------------\n")

    # 3. Fetch the current unit rate
    rate_url = f"{BASE_URL}/products/{product_code}/electricity-tariffs/{tariff_code}/standard-unit-rates/"
    rate_response = requests.get(rate_url) # Pricing endpoints are usually public
    rate_data = rate_response.json()

    if rate_data['results']:
        # The first result is the most current/latest price
        latest = rate_data['results'][0]
        price = latest['value_inc_vat']
        print(f"Current Unit Rate: {price}p/kWh (inc. VAT)")
        r.set('tariff', f"{price}")
        r.close()
    else:
        print("No pricing data found.")
        notify("Couldn't update tariff")

if __name__ == "__main__":
    get_current_unit_rate()