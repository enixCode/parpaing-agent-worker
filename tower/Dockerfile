FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . ./tower/

# Profiles and templates are mounted as volumes
EXPOSE 8420

CMD ["uvicorn", "tower.main:app", "--host", "0.0.0.0", "--port", "8420", "--proxy-headers", "--forwarded-allow-ips", "*"]
