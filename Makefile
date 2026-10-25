PYTHON ?= python3
VENV ?= .venv
PIP := $(VENV)/bin/pip
PYTHON_VENV := $(VENV)/bin/python
ENV_FILE ?= .env
STACK_TEST_ENV_FILE ?= .env.stack-test
OPENBAO_UID ?= $(shell id -u)
OPENBAO_GID ?= $(shell id -g)
export OPENBAO_UID OPENBAO_GID
DEV_COMPOSE_OVERRIDE ?=
DEV_COMPOSE = docker compose --project-directory . --env-file "$(ENV_FILE)" -f docker/docker-compose.yml $(if $(DEV_COMPOSE_OVERRIDE),-f "$(DEV_COMPOSE_OVERRIDE)",)
IMAGE_REPOSITORY ?= amazing-http-logger
FRONTEND_IMAGE_REPOSITORY ?= amazing-http-logger-dashboard
IMAGE_TAG ?= dev
HELM_RELEASE ?= logger
KUBE_NAMESPACE ?= logger
KUBE_CONTEXT ?=
KUBE_LOCAL_PORT ?= 9090
KUBE_SERVICE ?= $(HELM_RELEASE)-amazing-http-logger
HELM_CHART := helm/amazing-http-logger
HELM_VALUES ?= $(HELM_CHART)/values-development.yaml
HELM_ARGS ?=
HELM_CONTEXT = $(if $(KUBE_CONTEXT),--kube-context "$(KUBE_CONTEXT)",)
KUBECTL_CONTEXT = $(if $(KUBE_CONTEXT),--context "$(KUBE_CONTEXT)",)

install:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install -r requirements.txt

.test:
	$(PYTHON_VENV) -m pytest -q

test: .test

test-stack:
	STACK_TEST_ENV_FILE="$(STACK_TEST_ENV_FILE)" STACK_TEST_ENABLED=true $(PYTHON_VENV) -m pytest -q integration_tests/test_stack.py

lint:
	$(PYTHON_VENV) -m ruff check app tests integration_tests

frontend-install:
	npm --prefix frontend ci

frontend-dev: FRONTEND_PORT ?= 3000
frontend-dev:
	PORT="$(FRONTEND_PORT)" npm --prefix frontend run dev

frontend-build:
	npm --prefix frontend run build

frontend-start: FRONTEND_PORT ?= 3000
frontend-start:
	PORT="$(FRONTEND_PORT)" npm --prefix frontend run start

frontend-test:
	npm --prefix frontend test
	npm --prefix frontend run lint
	npm --prefix frontend run format:check

frontend-test-browser:
	npm --prefix frontend run build
	npm --prefix frontend run test:e2e

run:
	ENV_FILE="$(ENV_FILE)" "$(PYTHON_VENV)" -m app

worker:
	ENV_FILE="$(ENV_FILE)" "$(PYTHON_VENV)" -m app.worker

proxy-up:
	$(DEV_COMPOSE) up -d --wait proxy

proxy-down:
	$(DEV_COMPOSE) stop proxy

redis-up:
	$(DEV_COMPOSE) up -d --wait redis

redis-down:
	$(DEV_COMPOSE) stop redis

mongo-up:
	$(DEV_COMPOSE) up -d --wait mongo

mongo-down:
	$(DEV_COMPOSE) stop mongo

openbao-up:
	ENV_FILE="$(ENV_FILE)" "$(PYTHON_VENV)" -m app.kms prepare-dev
	$(DEV_COMPOSE) up -d --wait openbao
	ENV_FILE="$(ENV_FILE)" "$(PYTHON_VENV)" -m app.kms bootstrap-dev

openbao-down:
	$(DEV_COMPOSE) stop openbao

dev-up: mongo-up openbao-up redis-up proxy-up

dev-down:
	$(DEV_COMPOSE) down

dev: dev-up
	$(MAKE) run

image:
	docker build -f docker/Dockerfile -t "$(IMAGE_REPOSITORY):$(IMAGE_TAG)" .

frontend-image:
	docker build -f docker/frontend.Dockerfile -t "$(FRONTEND_IMAGE_REPOSITORY):$(IMAGE_TAG)" .

helm-lint:
	helm lint --strict $(HELM_CHART) -f "$(HELM_VALUES)" $(HELM_ARGS)

helm-template:
	helm template "$(HELM_RELEASE)" $(HELM_CHART) --namespace "$(KUBE_NAMESPACE)" -f "$(HELM_VALUES)" --set-string image.repository="$(IMAGE_REPOSITORY)" --set-string image.tag="$(IMAGE_TAG)" --set-string frontend.image.repository="$(FRONTEND_IMAGE_REPOSITORY)" --set-string frontend.image.tag="$(IMAGE_TAG)" $(HELM_ARGS)

helm-up:
	helm upgrade --install "$(HELM_RELEASE)" $(HELM_CHART) $(HELM_CONTEXT) --namespace "$(KUBE_NAMESPACE)" --create-namespace -f "$(HELM_VALUES)" --set-string image.repository="$(IMAGE_REPOSITORY)" --set-string image.tag="$(IMAGE_TAG)" --set-string frontend.image.repository="$(FRONTEND_IMAGE_REPOSITORY)" --set-string frontend.image.tag="$(IMAGE_TAG)" $(HELM_ARGS) --wait --wait-for-jobs --timeout 10m

helm-test:
	helm test "$(HELM_RELEASE)" $(HELM_CONTEXT) --namespace "$(KUBE_NAMESPACE)" --logs --timeout 2m

helm-down:
	helm uninstall "$(HELM_RELEASE)" $(HELM_CONTEXT) --namespace "$(KUBE_NAMESPACE)" --wait

helm-port-forward:
	kubectl $(KUBECTL_CONTEXT) --namespace "$(KUBE_NAMESPACE)" port-forward "service/$(KUBE_SERVICE)" "$(KUBE_LOCAL_PORT):http"

.PHONY: install test test-stack lint frontend-install frontend-dev frontend-build frontend-start frontend-test frontend-test-browser run worker proxy-up proxy-down redis-up redis-down mongo-up mongo-down openbao-up openbao-down dev-up dev-down dev image frontend-image helm-lint helm-template helm-up helm-test helm-down helm-port-forward
