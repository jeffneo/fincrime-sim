SHELL := /bin/bash

# docker compose reads .env on its own, but make does not, so without this the
# help text and cypher-shell invocations below would use the defaults while the
# containers use the .env values - printing URLs that point at the wrong ports.
-include .env
export

COMPOSE := docker compose
SCALE ?= dev
DB ?= fincrime
NEO4J_PASSWORD ?= fincrimefincrime
NEO4J_BOLT_PORT ?= 7687
CYPHER := $(COMPOSE) exec -T neo4j cypher-shell -u neo4j -p $(NEO4J_PASSWORD)

.PHONY: help up down nes nes-down generate validate export load import-mount-ok \
        clean-import all \
        check test lint rbac-check shell logs stats clean

help:
	@echo "Pipeline, in order:"
	@echo "  make up                    start Neo4j Enterprise (http://localhost:7474)"
	@echo "  make nes                   start Enterprise Studio + provision roles (:8080)"
	@echo "  make generate SCALE=dev    generate the dataset as Parquet"
	@echo "  make validate SCALE=dev    statistical fidelity + privacy audit"
	@echo "  make export SCALE=dev      stage neo4j-admin import CSV"
	@echo "  make load SCALE=dev        bulk-import into Neo4j, then apply constraints"
	@echo "  make all SCALE=dev         generate + validate + export + load"
	@echo ""
	@echo "  make check                 lint + tests"
	@echo "  make rbac-check            prove fincrime_demo cannot read ground truth"
	@echo ""
	@echo "  make stats / logs / shell / down"
	@echo "  make clean                 DESTRUCTIVE: drops the graph and out/"
	@echo ""
	@echo "Presets: SCALE=dev (10K entities, 3mo) | SCALE=mvp (100K, 12mo)"

# --- stack ------------------------------------------------------------------

up:
	@test -f "$${GDS_LICENSE_PATH:-./gds.license}" || { \
		echo "GDS license not found at $${GDS_LICENSE_PATH:-./gds.license}"; \
		echo "Without it the plugin loads in the community tier, which blocks"; \
		echo "algorithms the mule-network demo needs. Set GDS_LICENSE_PATH in .env."; \
		exit 1; }
	$(COMPOSE) up -d neo4j
	@echo "waiting for neo4j..."
	@until [ "$$($(COMPOSE) ps neo4j --format '{{.Health}}' 2>/dev/null)" = "healthy" ]; do \
		printf "."; sleep 3; \
	done; echo " ready"
	@echo "  Neo4j:  http://localhost:$${NEO4J_HTTP_PORT:-7474}  (neo4j / $(NEO4J_PASSWORD))"

nes:
	@test -f "$${NES_LICENSE_PATH:-./nes.license}" || { \
		echo "NES license not found at $${NES_LICENSE_PATH:-./nes.license}"; exit 1; }
	$(COMPOSE) --profile nes up -d
	@echo "waiting for Enterprise Studio..."
	@until [ "$$($(COMPOSE) ps enterprise-studio --format '{{.Health}}' 2>/dev/null)" = "healthy" ]; do \
		printf "."; sleep 5; \
	done; echo " ready"
	@echo ""
	@echo "  Studio:    http://localhost:$${NES_PORT:-8080}"
	@echo "  Admin:     neo4j / $(NEO4J_PASSWORD)      (sees ground truth, runs scoring)"
	@echo "  Demo:      analyst / analystanalyst       (ground truth denied)"

nes-down:
	$(COMPOSE) --profile nes stop enterprise-studio nes-init

down:
	$(COMPOSE) --profile nes down

# --- pipeline ---------------------------------------------------------------

generate:
	uv run fincrime generate --scale $(SCALE)

validate:
	uv run fincrime validate --scale $(SCALE)

export:
	uv run fincrime export-csv --scale $(SCALE)

# The offline importer requires the target database stopped. Studio holds no
# connection to `fincrime` (its asset store is `tools-storage`), so it can stay
# up across a reload.
#
# --user neo4j is load-bearing. `docker compose exec` defaults to root, and the
# importer writes the store files as whoever runs it. Root-owned store files
# cannot be opened by the server, which runs as neo4j, so the database comes
# back `offline` with a bare filename as its status message and no error in the
# log. The chown recovers a database already broken that way.
load: import-mount-ok
	$(CYPHER) -d system "STOP DATABASE $(DB) WAIT"
	$(COMPOSE) exec -T --user root neo4j chown -R neo4j:neo4j /data/databases /data/transactions
	$(COMPOSE) exec -T --user neo4j neo4j sh /import/import.sh
	$(CYPHER) -d system "START DATABASE $(DB) WAIT"
	@echo "applying constraints..."
	$(CYPHER) -d $(DB) -f /cypher/constraints.cypher
	@$(MAKE) --no-print-directory clean-import
	@echo
	@$(MAKE) --no-print-directory stats

# Staged CSV is disposable once the store is built - 14GB of it at the mvp
# preset, against a 19GB store and 1.8GB of Parquet. The directory itself must
# survive: it is bind-mounted into the container, and deleting it would leave
# the container holding a deleted inode (see import-mount-ok).
clean-import:
	@if [ -d out/import ]; then \
		size=$$(du -sh out/import 2>/dev/null | cut -f1); \
		find out/import -mindepth 1 -delete; \
		echo "cleared $$size of staged import CSV (regenerate with: make export)"; \
	fi

# `out/import` is bind-mounted into the container. Deleting the host directory
# while the container runs leaves it holding the deleted inode, so the staged
# files are invisible inside even though they are plainly there on the host.
# Re-establishing the mount needs a container restart, which is cheap and
# beats the confusing "cannot open /import/import.sh: No such file".
import-mount-ok:
	@test -f out/import/import.sh || { \
		echo "out/import/import.sh is missing. Run: make export SCALE=$(SCALE)"; exit 1; }
	@if ! $(COMPOSE) exec -T neo4j test -f /import/import.sh 2>/dev/null; then \
		echo "import mount is stale (out/ was deleted while the container ran); restarting neo4j..."; \
		$(COMPOSE) restart neo4j >/dev/null; \
		until [ "$$($(COMPOSE) ps neo4j --format '{{.Health}}' 2>/dev/null)" = "healthy" ]; do \
			printf "."; sleep 3; \
		done; echo " ready"; \
	fi

all: generate validate export load

# --- verification -----------------------------------------------------------

check: lint test

lint:
	uv run ruff check src tests
	uv run ruff format --check src tests

test:
	uv run pytest

# Proves D5' against the running database rather than against the Cypher
# source. This is the check that matters before any dataset is handed to a
# customer: if it fails, the demo user can read the answer key.
rbac-check:
	NEO4J_URI=bolt://localhost:$(NEO4J_BOLT_PORT) uv run pytest tests/test_rbac.py -v --run-neo4j

stats:
	@$(CYPHER) -d $(DB) --format plain \
		"MATCH (n) RETURN labels(n) AS labels, count(*) AS nodes ORDER BY nodes DESC"
	@$(CYPHER) -d $(DB) --format plain \
		"MATCH ()-[r]->() RETURN type(r) AS type, count(*) AS rels ORDER BY rels DESC"

shell:
	$(COMPOSE) exec neo4j cypher-shell -u neo4j -p $(NEO4J_PASSWORD) -d $(DB)

logs:
	$(COMPOSE) logs -f neo4j

# Order matters: bring the stack down before deleting out/, so the import
# bind mount is released rather than orphaned.
clean:
	$(COMPOSE) --profile nes down -v
	rm -rf out/
