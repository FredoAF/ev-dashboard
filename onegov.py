import requests, os
from dotenv import load_dotenv
import json
load_dotenv()
import redis

def notify(msg):
    requests.post(
        "https://ntfy.sh/farren-renault",
        data=msg.encode(encoding="utf-8"),
        headers={"Title": "R5", "Tags": "bug"},
    )

station_id = "9dfd7ba2235eb23f04d99548373ad1254c64b2abb45e366085281d1d1dd4e999"
access_token = ""
CLIENT_SECRET = os.environ.get('ONEGOV_CLIENT_SECRET')
CLIENT_ID = os.environ.get('ONEGOV_CLIENT_ID')
REDIS = os.environ.get('REDIS_HOST')
base_url = "https://www.fuel-finder.service.gov.uk"

r = redis.Redis(host=REDIS, port=6379, db=0)

payload = {
    "client_id": CLIENT_ID,
    "client_secret": CLIENT_SECRET,
    "grant_type": "client_credentials",
    "scope": "fuelfinder.read"
}

response = requests.post(base_url+"/api/v1/oauth/generate_access_token", json=payload, headers={"accept": "application/json"})

# Checking the result
if response.status_code == 200:
    token_data = response.json()
    access_token = token_data['data']['access_token']
else:
    print(f"Failed with status code: {response.status_code}")
    print(response.text)

response = requests.get(base_url+"/api/v1/pfs/fuel-prices?batch-number=2", headers = {"Authorization": f"Bearer {access_token}","Content-Type": "application/json"})
if response.status_code == 200:
    # print(json.dumps(response.json(), indent=4))
    for station in response.json():
        # print(station['node_id'])
        if station['node_id'] == station_id:
            for fuel_price in station['fuel_prices']:
                if fuel_price['fuel_type'] == "E10":
                    price = round(float(fuel_price['price'])/100, 3)
                    print(f"New Price Per Litre at Wolverton Tesco: {price}")
                    r.set('pricePerLitre', price)
                    r.close()
else:
    print(response.content)
    notify("Couldn't update fuel")