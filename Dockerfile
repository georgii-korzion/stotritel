FROM python:3.12-slim

# без буферизации stdout — иначе логи не видны в Railway
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY fimex_monitor ./fimex_monitor

# Пользователя не меняем: том Railway монтируется от root, бот пишет в /app/data.
CMD ["python", "-m", "fimex_monitor", "run"]
