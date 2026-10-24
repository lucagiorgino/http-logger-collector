FROM python:3.11-slim

ARG APP_PORT=8000

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_ENV=production \
    APP_HOST=0.0.0.0 \
    APP_PORT=${APP_PORT} \
    APP_RELOAD=false \
    MONGO_ALLOW_PRIMARY_READS=false

COPY requirements.txt pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY tests ./tests

EXPOSE ${APP_PORT}

CMD ["python", "-m", "app"]
