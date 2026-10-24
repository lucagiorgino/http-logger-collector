# Runbook

## Prerequisites
- Python 3.11+
- Docker and Docker Compose
- MongoDB or a local dockerized MongoDB instance

## Local setup
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## Run the app
```bash
uvicorn app.main:app --reload
```

## Run tests
```bash
pytest -q
```

## Troubleshooting
- If MongoDB is unavailable, start Docker Compose and verify the connection string.
- On startup the API creates the `traffic_logs` indexes and fails fast if MongoDB is unavailable.
- `MONGO_SERVER_SELECTION_TIMEOUT_MS` controls how long MongoDB connectivity checks wait.
- If auth requests fail, check the `.env` values and token configuration.
- If imports fail, ensure dependencies are installed in the active virtual environment.
