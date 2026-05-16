# Renault Dashboard

Okay I have a redis and a grafana hooked up to that, works well

Todo:
- [x] Work out how to store time series data in redis for charges
- [x] Cronjob to fetch latest price of petrol from gov api and post to redis
- [x] Cronjob to get the latest tarrif rate from octopus api
- [x] Cronjob to run the renault stats and update stats,
