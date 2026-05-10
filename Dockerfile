FROM python

ENV PYTHONUNBUFFERED=true
RUN pip install redis renault-api aiohttp

COPY . /app
WORKDIR /app

ENTRYPOINT ["python", "/app/renault.py"]