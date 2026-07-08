FROM python:3.12-alpine AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SMART_STACKER_STATE_FILE=/data/.immich-smart-stacker-state.json

WORKDIR /app

RUN apk add --no-cache --virtual .build-deps \
        build-base \
        musl-dev \
        linux-headers \
        libjpeg-turbo-dev \
        zlib-dev

COPY requirements.txt immich-smart-stacker.py ./

RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

FROM python:3.12-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SMART_STACKER_STATE_FILE=/data/.immich-smart-stacker-state.json

WORKDIR /app

RUN apk add --no-cache \
        libstdc++ \
        libgcc \
        libjpeg-turbo \
    zlib \
    ffmpeg \
    && addgroup -S app \
    && adduser -S -G app app \
    && mkdir -p /data \
    && chown -R app:app /app /data

COPY --from=builder /wheels /wheels
COPY requirements.txt immich-smart-stacker.py ./
COPY immich_smart_stacker ./immich_smart_stacker

RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels

VOLUME ["/data"]

USER app

ENTRYPOINT ["python", "-m", "immich_smart_stacker"]
