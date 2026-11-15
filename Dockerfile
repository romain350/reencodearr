FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY agent ./agent
ENV SERIES_DIR=/series MOVIES_DIR=/movies DATA_DIR=/data PYTHONUNBUFFERED=1
EXPOSE 8095
# un seul processus : la file d'attente et les scans vivent en memoire du serveur
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8095", "--workers", "1"]
