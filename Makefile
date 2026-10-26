PYTHON ?= python3
.DEFAULT_GOAL := help
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
COMPOSE = ENV_FILE="$(ENV_FILE)" docker compose --project-directory . --env-file "$(ENV_FILE)" -f docker/docker-compose.yml
PROXY_SERVICE ?= proxy
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

help:
	@printf '%s\n' \
	  'Local development (see docs/RUNBOOK.md):' \
	  '  local-infra-up          Start Docker dependencies; provision credentials, keys, queues and bucket' \
	  '  local-containers-up     Start the full Docker stack, including collector, workers, frontend and proxy' \
	  '  local-workers-up        Start parser and loader containers plus dependencies' \
	  '  local-apps-stop         Stop application containers/proxy; preserve running dependencies and keys' \
	  '  local-containers-down   Remove local containers/network; retain Mongo/MinIO volumes (OpenBao keys are lost)' \
	  '  local-collector-run     Run ONLY the collector natively in this terminal using .venv and ENV_FILE' \
	  '  local-parser-run        Run the parser natively in this terminal' \
	  '  local-loader-run        Run the loader natively in this terminal' \
	  '  local-frontend-dev      Run the Next.js dev server natively in this terminal' \
	  '  local-native-run        Start dependencies/proxy, then supervise native collector/parser/loader' \
	  '  local-proxy-native-up   Start the proxy for native applications (PROXY_SERVICE=proxy-host on native Linux)' \
	  '  local-cleanup           Run one-shot container cleanup; requires UPLOAD_ID=<stored receipt ID>' \
	  '  compose-config          Validate every profile without printing environment values' \
	  '  compose-ps / compose-logs  Show containers / follow logs' \
	  'Legacy run/dev/spool-* and frontend-dev commands remain compatibility aliases.'

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

local-frontend-dev: FRONTEND_PORT ?= 3000
local-frontend-dev:
	PORT="$(FRONTEND_PORT)" npm --prefix frontend run dev

frontend-dev: local-frontend-dev

frontend-build:
	npm --prefix frontend run build

local-frontend-start: FRONTEND_PORT ?= 3000
local-frontend-start:
	PORT="$(FRONTEND_PORT)" npm --prefix frontend run start

frontend-start: local-frontend-start

frontend-test:
	npm --prefix frontend test
	npm --prefix frontend run lint
	npm --prefix frontend run format:check

frontend-test-browser:
	npm --prefix frontend run build
	npm --prefix frontend run test:e2e

local-collector-run:
	ENV_FILE="$(ENV_FILE)" "$(PYTHON_VENV)" -m app

local-parser-run:
	ENV_FILE="$(ENV_FILE)" "$(PYTHON_VENV)" -m app.spool_worker parse

local-loader-run:
	ENV_FILE="$(ENV_FILE)" "$(PYTHON_VENV)" -m app.spool_worker load

run: local-collector-run
spool-parser: local-parser-run
spool-loader: local-loader-run

spool-dev-up:
	APP_ENV=development ENV_FILE="$(ENV_FILE)" "$(PYTHON_VENV)" -m app.spool_dev prepare
	$(COMPOSE) --profile spool up -d --build --wait
	$(COMPOSE) build pubsub-init minio-init
	$(COMPOSE) run --rm --no-deps pubsub-init
	$(COMPOSE) run --rm --no-deps minio-init

spool-dev-down:
	$(COMPOSE) stop minio spool-bridge pubsub

local-proxy-native-up:
	@case "$(PROXY_SERVICE)" in proxy|proxy-host) ;; *) echo "PROXY_SERVICE must be proxy or proxy-host" >&2; exit 1;; esac
	$(COMPOSE) stop proxy-containers $(if $(filter proxy-host,$(PROXY_SERVICE)),proxy,proxy-host)
	$(COMPOSE) up -d --wait "$(PROXY_SERVICE)"

proxy-up: local-proxy-native-up

proxy-down:
	$(COMPOSE) stop proxy proxy-host proxy-containers

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

local-infra-up:
	APP_ENV=development ENV_FILE="$(ENV_FILE)" "$(PYTHON_VENV)" -m app.kms prepare-dev
	APP_ENV=development ENV_FILE="$(ENV_FILE)" "$(PYTHON_VENV)" -m app.spool_dev prepare
	$(COMPOSE) up -d --no-recreate --wait openbao
	APP_ENV=development ENV_FILE="$(ENV_FILE)" "$(PYTHON_VENV)" -m app.kms bootstrap-dev
	$(COMPOSE) --profile infra up -d --build --wait mongo pubsub spool-bridge minio
	$(COMPOSE) build pubsub-init minio-init
	$(COMPOSE) run --rm --no-deps pubsub-init
	$(COMPOSE) run --rm --no-deps minio-init

local-containers-up: local-infra-up
	$(COMPOSE) stop proxy proxy-host
	$(COMPOSE) --profile full up -d --build --wait --no-deps --force-recreate collector parser loader frontend proxy-containers

local-workers-up: local-infra-up
	$(COMPOSE) --profile infra --profile workers up -d --build --wait --no-deps --force-recreate parser loader

local-apps-stop:
	$(COMPOSE) --profile "*" stop collector parser loader frontend proxy-containers

local-containers-down:
	$(COMPOSE) --profile "*" down

local-cleanup:
	@test -n "$(UPLOAD_ID)" || { echo 'UPLOAD_ID=<stored receipt ID> is required' >&2; exit 2; }
	$(COMPOSE) --profile infra --profile maintenance run --rm cleaner

dev-up: local-infra-up local-proxy-native-up
dev-down: local-containers-down

compose-config:
	$(COMPOSE) --profile "*" config --quiet

compose-ps:
	$(COMPOSE) --profile "*" ps

compose-logs:
	$(COMPOSE) --profile "*" logs --follow --tail 100

local-native-run: dev-up
	ENV_FILE="$(ENV_FILE)" bash -ec '\
	  "$(PYTHON_VENV)" -m app.spool_worker parse & parse_pid=$$!; \
	  "$(PYTHON_VENV)" -m app.spool_worker load & load_pid=$$!; \
	  "$(PYTHON_VENV)" -m app & api_pid=$$!; \
	  trap '\''kill "$$parse_pid" "$$load_pid" "$$api_pid" 2>/dev/null || true; wait'\'' EXIT; \
	  trap '\''exit 130'\'' INT TERM; \
	  wait -n "$$parse_pid" "$$load_pid" "$$api_pid"'

dev: local-native-run

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

.PHONY: help local-infra-up local-containers-up local-workers-up local-apps-stop local-containers-down local-cleanup local-collector-run local-parser-run local-loader-run local-frontend-dev local-frontend-start local-proxy-native-up local-native-run install gcp-install gcp-check gcp-deploy gcp-secrets-bootstrap test test-stack lint frontend-install frontend-dev frontend-build frontend-start frontend-test frontend-test-browser run spool-parser spool-loader spool-dev-up spool-dev-down proxy-up proxy-down mongo-up mongo-down openbao-up openbao-down dev-up dev-down dev compose-config compose-ps compose-logs image frontend-image helm-lint helm-template helm-up helm-test helm-down helm-port-forward
