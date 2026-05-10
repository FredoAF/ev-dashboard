# Renault Dashboard

Okay I have a redis and a grafana hooked up to that, works well

Todo:
- Work out how to store time series data in redis for charges
- Cronjob to fetch latest price of petrol from gov api and post to redis
- Cronjob to get the latest tarrif rate from octopus api
- Cronjob to run the renault stats and update stats,

i need to decide whether I'm going to track every charge, work out the cost of that charge and the equivalent petrol saving, and update a savings collumn

Or just look at the miles i've done, and work out the cost of the difference of those miles based on the current tarif and price per mile.