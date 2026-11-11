SHELL := /bin/bash
.DEFAULT_GOAL := help

PG_IMAGE  := postgres:16-alpine
PG_ENV    := -e POSTGRES_USER=netlab -e POSTGRES_PASSWORD=netlab -e POSTGRES_DB=netlab
TEST_DB   ?= postgresql+psycopg://netlab:netlab@127.0.0.1:55433/netlab
API       ?= http://127.0.0.1:8000
export NETLAB_API := $(API)

.PHONY: help
help: ## list targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

# --- dev ------------------------------------------------------------------
.PHONY: lint test test-db
lint: ## ruff + mypy
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy app lab

test-db:
	@docker inspect netlab-pg-test >/dev/null 2>&1 || docker run -d --name netlab-pg-test $(PG_ENV) \
	  -p 127.0.0.1:55433:5432 --tmpfs /var/lib/postgresql/data $(PG_IMAGE) >/dev/null
	@docker start netlab-pg-test >/dev/null
	@until docker exec netlab-pg-test pg_isready -h 127.0.0.1 -U netlab -q; do sleep 0.5; done

test: test-db ## unit + API + snapshot tests (throwaway postgres in docker)
	NETLAB_DATABASE_URL=$(TEST_DB) uv run pytest -m "not lab"

# --- lab ------------------------------------------------------------------
.PHONY: images up seed deploy demo lab-test down
images: ## build API and lab images
	docker build -q -t netlab-api .
	docker build -q -t netlab-router lab/images/router
	docker build -q -t netlab-host lab/images/host

up: images ## postgres + IPAM API on :8000
	@docker network inspect netlab-mgmt >/dev/null 2>&1 || docker network create netlab-mgmt >/dev/null
	@docker inspect netlab-pg >/dev/null 2>&1 || docker run -d --name netlab-pg --network netlab-mgmt \
	  $(PG_ENV) -p 127.0.0.1:55432:5432 $(PG_IMAGE) >/dev/null
	@until docker exec netlab-pg pg_isready -h 127.0.0.1 -U netlab -q; do sleep 0.5; done
	@docker rm -f netlab-api >/dev/null 2>&1 || true
	docker run -d --name netlab-api --network netlab-mgmt -p 127.0.0.1:8000:8000 \
	  -e NETLAB_DATABASE_URL=postgresql+psycopg://netlab:netlab@netlab-pg:5432/netlab netlab-api >/dev/null
	@timeout 60 bash -c 'until curl -sf $(API)/health >/dev/null; do sleep 0.5; done' \
	  || { docker logs netlab-api; exit 1; }
	@echo "API ready: $(API)/docs"

seed: ## load lab topology into IPAM via the API
	uv run python -m lab.seed

deploy: ## render configs from the API and start the docker lab
	uv run python -m lab.deploy

demo: up seed deploy ## everything from scratch: API, IPAM data, routers, NAT, DNS

lab-test: ## network tests against the running lab (ping/traceroute/nft/dig)
	NETLAB_LAB=1 uv run pytest -m lab -v

down: ## remove lab, API and postgres
	-uv run python -m lab.deploy --destroy
	-docker rm -f netlab-api netlab-pg netlab-pg-test >/dev/null 2>&1
	-docker network rm netlab-mgmt >/dev/null 2>&1
