FROM python:3.13-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_ENV=production \
    APP_HOST=0.0.0.0 \
    APP_PORT=9090 \
    APP_RELOAD=false \
    MONGO_ALLOW_PRIMARY_READS=false

COPY requirements.txt /tmp/application-requirements.txt
COPY infra/gcp/runtime/requirements.txt /tmp/cloud-requirements.txt
RUN pip install --no-cache-dir -r /tmp/application-requirements.txt -r /tmp/cloud-requirements.txt \
    && groupadd --gid 10001 logger \
    && useradd --uid 10001 --gid 10001 --no-create-home logger

COPY app /app/app
COPY infra/gcp/runtime /app/cloud_runtime
# Only this cloud image uses the GCP implementation of the existing KMS interface.
COPY infra/gcp/runtime/openbao.py /app/app/core/openbao.py
USER 10001:10001
EXPOSE 9090
CMD ["uvicorn", "cloud_runtime.api:app", "--host", "0.0.0.0", "--port", "9090", "--no-proxy-headers"]
