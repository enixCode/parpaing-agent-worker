FROM python:3.12-slim

WORKDIR /app

COPY tower/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY tower/ ./tower/
COPY profiles/ ./profiles/
COPY templates/ ./templates/
COPY engines/ ./engines/
COPY hooks/ ./hooks/
COPY ui/ ./ui/

EXPOSE 8420

CMD ["uvicorn", "tower.main:app", "--host", "0.0.0.0", "--port", "8420", "--proxy-headers", "--forwarded-allow-ips", "*"]
