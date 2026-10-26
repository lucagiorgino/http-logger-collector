PYTHON ?= python3
VENV ?= .venv
PIP := $(VENV)/bin/pip
PYTHON_VENV := $(VENV)/bin/python
ENV_FILE ?= .env
PROJECT_ID ?=
ENVIRONMENT ?= production
GCP_VENV ?= infra/gcp/.venv
GCP_ARGS ?=
STACK_TEST_ENV_FILE ?= .env.stack-test
OPENBAO_UID ?= $(shell id -u)
OPENBAO_GID ?= $(shell id -g)
export OPENBAO_UID OPENBAO_GID
COMPOSE = docker compose --project-directory . --env-file "$(ENV_FILE)" -f docker/docker-compose.yml
PROXY_SERVICE ?= proxy
SPOOL_DEV_ENV = APP_ENV=development SPOOL_STORAGE_BACKEND=minio SPOOL_ENDPOINT_URL=http://localhost:29000 PUBSUB_EMULATOR_HOST=localhost:28681 SPOOL_BUCKET=traffic-uploads SPOOL_EVENT_TOPIC=projects/logger-local/topics/uploads SPOOL_SUBSCRIPTION=projects/logger-local/subscriptions/uploads SPOOL_SILVER_TOPIC=projects/logger-local/topics/uploads-silver SPOOL_SILVER_SUBSCRIPTION=projects/logger-local/subscriptions/uploads-silver SPOOL_PARSE_DLQ_TOPIC=projects/logger-local/topics/parse-dlq SPOOL_LOAD_DLQ_TOPIC=projects/logger-local/topics/load-dlq
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

gcp-secrets-bootstrap:
	@"$(PYTHON_VENV)" -m infra.gcp.bootstrap_secrets --project="$(PROJECT_ID)" --environment="$(ENVIRONMENT)"

gcp-install:
	$(PYTHON) -m venv "$(GCP_VENV)"
	"$(GCP_VENV)/bin/pip" install -r requirements.txt -r infra/gcp/runtime/requirements.txt

gcp-check:
	@"$(GCP_VENV)/bin/python" -m infra.gcp.deploy --check $(GCP_ARGS)

gcp-deploy:
	@"$(GCP_VENV)/bin/python" -m infra.gcp.deploy $(GCP_ARGS)

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

spool-parser:
	ENV_FILE="$(ENV_FILE)" "$(PYTHON_VENV)" -m app.spool_worker parse

spool-loader:
	ENV_FILE="$(ENV_FILE)" "$(PYTHON_VENV)" -m app.spool_worker load

spool-dev-up:
	ENV_FILE="$(ENV_FILE)" "$(PYTHON_VENV)" -m app.spool_dev prepare
	$(COMPOSE) up -d --build --wait pubsub spool-bridge minio
	$(SPOOL_DEV_ENV) ENV_FILE="$(ENV_FILE)" "$(PYTHON_VENV)" -m app.spool_dev init
	$(COMPOSE) --profile spool run --no-deps --build --rm minio-init

spool-dev-down:
	$(COMPOSE) stop minio spool-bridge pubsub

proxy-up:
	@case "$(PROXY_SERVICE)" in proxy|proxy-host) ;; *) echo "PROXY_SERVICE must be proxy or proxy-host" >&2; exit 1;; esac
	$(COMPOSE) stop $(if $(filter proxy-host,$(PROXY_SERVICE)),proxy,proxy-host)
	$(COMPOSE) up -d --wait "$(PROXY_SERVICE)"

proxy-down:
	$(COMPOSE) stop proxy proxy-host

mongo-up:
	$(COMPOSE) up -d --wait mongo

mongo-down:
	$(COMPOSE) stop mongo

openbao-up:
	ENV_FILE="$(ENV_FILE)" "$(PYTHON_VENV)" -m app.kms prepare-dev
	$(COMPOSE) up -d --wait openbao
	ENV_FILE="$(ENV_FILE)" "$(PYTHON_VENV)" -m app.kms bootstrap-dev

openbao-down:
	$(COMPOSE) stop openbao

dev-up: mongo-up openbao-up proxy-up spool-dev-up

dev-down:
	$(COMPOSE) --profile "*" down

compose-config:
	$(COMPOSE) --profile "*" config --quiet

compose-ps:
	$(COMPOSE) --profile "*" ps

compose-logs:
	$(COMPOSE) --profile "*" logs --follow --tail 100

dev: dev-up
	$(SPOOL_DEV_ENV) ENV_FILE="$(ENV_FILE)" bash -ec '\
	  "$(PYTHON_VENV)" -m app.spool_worker parse & parse_pid=$$!; \
	  "$(PYTHON_VENV)" -m app.spool_worker load & load_pid=$$!; \
	  "$(PYTHON_VENV)" -m app & api_pid=$$!; \
	  trap '\''kill "$$parse_pid" "$$load_pid" "$$api_pid" 2>/dev/null || true; wait'\'' EXIT; \
	  trap '\''exit 130'\'' INT TERM; \
	  wait -n "$$parse_pid" "$$load_pid" "$$api_pid"'

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

.PHONY: install gcp-install gcp-check gcp-deploy gcp-secrets-bootstrap test test-stack lint frontend-install frontend-dev frontend-build frontend-start frontend-test frontend-test-browser run spool-parser spool-loader spool-dev-up spool-dev-down proxy-up proxy-down mongo-up mongo-down openbao-up openbao-down dev-up dev-down dev compose-config compose-ps compose-logs image frontend-image helm-lint helm-template helm-up helm-test helm-down helm-port-forward
