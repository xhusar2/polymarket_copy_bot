FROM python:3.12-slim-bookworm

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    STATE_FILE=/data/copy_state.json

RUN mkdir -p /data

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY copy_trader/ ./copy_trader/

ENTRYPOINT ["python", "-m", "copy_trader"]
