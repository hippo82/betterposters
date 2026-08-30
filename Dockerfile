FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY *.py ./

RUN pip install --no-cache-dir requests python-dotenv \
    && mkdir -p /app/data

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4)"]

# entrypoint drops to PUID:PGID (env) before running main.py
ENTRYPOINT ["python", "/app/entrypoint.py"]
