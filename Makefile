PYTHON ?= python3
VENV ?= .venv
PIP := $(VENV)/bin/pip
PYTHON_VENV := $(VENV)/bin/python

install:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install -r requirements.txt

.test:
	$(PYTHON_VENV) -m pytest -q

test: .test

lint:
	$(PYTHON_VENV) -m ruff check app tests

run:
	$(PYTHON_VENV) -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

docker-up:
	docker compose -f docker/docker-compose.yml up -d

docker-down:
	docker compose -f docker/docker-compose.yml down

.PHONY: install test lint run docker-up docker-down
