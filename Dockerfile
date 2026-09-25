FROM python:3.13-slim

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/
COPY web/ web/
COPY data/route_sample.json data/geocode_cache.json data/

ENV PYTHONUNBUFFERED=1
EXPOSE 8000
# Single worker: SQLite on the Azure Files share expects one writer (app runs with max 1 replica).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
