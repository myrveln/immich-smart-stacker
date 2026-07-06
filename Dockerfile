FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SMART_STACKER_STATE_FILE=/data/.immich-smart-stacker-state.json

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY immich-smart-stacker.py ./

VOLUME ["/data"]

ENTRYPOINT ["python", "/app/immich-smart-stacker.py"]
