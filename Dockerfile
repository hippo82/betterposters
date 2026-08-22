FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY main.py ./

RUN pip install --no-cache-dir requests python-dotenv

CMD ["python", "main.py"]
