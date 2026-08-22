FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    default-libmysqlclient-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir python-telegram-bot==20.* aiomysql pydantic google-genai Pillow

COPY . .

CMD ["python", "main.py"]