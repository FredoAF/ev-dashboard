import redis

r = redis.Redis(host='localhost', port=6379, db=0)

# r.set('test', 'hello world')

test = r.get('pricePerLitre')

print(test)