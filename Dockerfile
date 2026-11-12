FROM python:3.11.18-alpine3.23

ENV PYTHONUNBUFFERED=true
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app

# Default job; the fuel price CronJob overrides this with: python /app/fuel_price.py
ENTRYPOINT ["python", "/app/renault.py"]
