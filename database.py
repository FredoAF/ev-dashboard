import redis

r = redis.Redis(host='localhost', port=6379, db=0)

# r.set('accumCostElec', 37.34)
r.set('accumCostPetrol', 140)

# test = r.get('pricePerLitre')

# print(test)