PYTHON ?= python3
VENV ?= .venv
PIP := $(VENV)/bin/pip
PYTHON_VENV := $(VENV)/bin/python
ENV_FILE ?= .env
OPENBAO_UID ?= $(shell id -u)
OPENBAO_GID ?= $(shell id -g)
export OPENBAO_UID OPENBAO_GID
DEV_COMPOSE := docker compose --project-directory . --env-file "$(ENV_FILE)" -f docker/docker-compose.yml
IMAGE_REPOSITORY ?= amazing-http-logger
IMAGE_TAG ?= dev
HELM_RELEASE ?= logger
KUBE_NAMESPACE ?= logger
KUBE_CONTEXT ?=
KUBE_LOCAL_PORT ?= 18000
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

lint:
	$(PYTHON_VENV) -m ruff check app tests

run:
	ENV_FILE="$(ENV_FILE)" "$(PYTHON_VENV)" -m app

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

dev-up: mongo-up openbao-up

dev-down:
	$(DEV_COMPOSE) down

dev: dev-up
	$(MAKE) run

image:
	docker build -f docker/Dockerfile -t "$(IMAGE_REPOSITORY):$(IMAGE_TAG)" .

helm-lint:
	helm lint --strict $(HELM_CHART) -f "$(HELM_VALUES)" $(HELM_ARGS)

helm-template:
	helm template "$(HELM_RELEASE)" $(HELM_CHART) --namespace "$(KUBE_NAMESPACE)" -f "$(HELM_VALUES)" --set-string image.repository="$(IMAGE_REPOSITORY)" --set-string image.tag="$(IMAGE_TAG)" $(HELM_ARGS)

helm-up:
	helm upgrade --install "$(HELM_RELEASE)" $(HELM_CHART) $(HELM_CONTEXT) --namespace "$(KUBE_NAMESPACE)" --create-namespace -f "$(HELM_VALUES)" --set-string image.repository="$(IMAGE_REPOSITORY)" --set-string image.tag="$(IMAGE_TAG)" $(HELM_ARGS) --wait --wait-for-jobs --timeout 10m

helm-test:
	helm test "$(HELM_RELEASE)" $(HELM_CONTEXT) --namespace "$(KUBE_NAMESPACE)" --logs --timeout 2m

helm-down:
	helm uninstall "$(HELM_RELEASE)" $(HELM_CONTEXT) --namespace "$(KUBE_NAMESPACE)" --wait

helm-port-forward:
	kubectl $(KUBECTL_CONTEXT) --namespace "$(KUBE_NAMESPACE)" port-forward "service/$(KUBE_SERVICE)" "$(KUBE_LOCAL_PORT):http"

.PHONY: install test lint run mongo-up mongo-down openbao-up openbao-down dev-up dev-down dev image helm-lint helm-template helm-up helm-test helm-down helm-port-forward
