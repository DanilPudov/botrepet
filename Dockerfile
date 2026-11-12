# --- базовый образ ---
FROM python:3.12-slim

# Часовой пояс и кодировка (бот также ставит их сам в коде)
ENV TZ=Europe/Moscow \
    LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# сначала зависимости (слой кэшируется)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && find /usr/local/lib/python3.12/site-packages/googleapiclient/discovery_cache \
            -type f -name "*.json" -delete 2>/dev/null || true

# затем код
COPY *.py ./
COPY config.example.json ./

# state.json и config.json подкладываются при деплое (не в образе)
VOLUME ["/app/data"]

CMD ["python", "-u", "bot.py"]
