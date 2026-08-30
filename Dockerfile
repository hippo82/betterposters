FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY main.py ./

RUN pip install --no-cache-dir requests python-dotenv

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4)"]

CMD ["python", "main.py"]
