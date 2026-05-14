FROM python:alpine

ENV PYTHONUNBUFFERED=true
RUN pip install redis renault-api aiohttp requests load_dotenv

COPY . /app
WORKDIR /app

ENTRYPOINT ["python", "/app/renault.py"]