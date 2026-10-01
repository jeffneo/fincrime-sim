SHELL := /bin/bash

# docker compose reads .env on its own, but make does not, so without this the
# help text and cypher-shell invocations below would use the defaults while the
# containers use the .env values - printing URLs that point at the wrong ports.
-include .env
export

PY := uv run --quiet python3
COMPOSE := docker compose
SCALE ?= dev

# One database per preset. A load is a whole-store replacement
# (--overwrite-destination), so sharing a database name between presets means a
# dev smoke test silently destroys the mvp graph - which is exactly what
# happened once. Enterprise multi-database makes keeping both cheap.
ifeq ($(SCALE),mvp)
DB ?= fincrime
else
DB ?= fincrime-$(SCALE)
endif
NEO4J_PASSWORD ?= fincrimefincrime
NEO4J_BOLT_PORT ?= 7687
CYPHER := $(COMPOSE) exec -T neo4j cypher-shell -u neo4j -p $(NEO4J_PASSWORD)

.PHONY: help up down nes nes-down generate validate export load import-mount-ok \
        load-guard stamp clean-import all \
        check test lint rbac-check shell logs stats clean databases \
        aura-guard aura-dump aura-push aura-setup aura-bench bench \
        release release-check backup restore

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
	@echo "Demo target (the mvp graph is not interactive on a laptop):"
	@echo "  make aura-push SCALE=mvp   dump and upload to the Aura instance in .env"
	@echo "  make aura-setup            roles, deny rules and demo users on Aura"
	@echo "  make aura-bench            time the demo query set as admin and analyst"
	@echo "  make bench TARGET=local    same timings against the local container"
	@echo ""
	@echo "Backup / restore (the Docker volume is disposable; these are not):"
	@echo "  make backup                dump fincrime + Studio's saved assets to backups/latest"
	@echo "  make restore               rebuild everything in Docker from backups/latest"
	@echo ""
	@echo "Release:"
	@echo "  make release SCALE=mvp     assemble releases/<version>/ (gated on release-check)"
	@echo "  make release-check         demo role runs the whole set; answer key unreadable"
	@echo ""
	@echo "  make stats / logs / shell / down"
	@echo "  make clean                 DESTRUCTIVE: drops the graph and out/"
	@echo ""
	@echo "Presets: SCALE=dev (10K entities, 3mo) | SCALE=mvp (100K, 12mo)"
	@echo "Each preset loads into its own database: mvp -> fincrime, dev -> fincrime-dev"
	@echo "  make databases             show what is loaded where"

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
load: import-mount-ok load-guard
	$(CYPHER) -d system "STOP DATABASE $(DB) WAIT"
	$(COMPOSE) exec -T --user root neo4j chown -R neo4j:neo4j /data/databases /data/transactions
	$(COMPOSE) exec -T --user neo4j neo4j sh /import/import.sh
	$(CYPHER) -d system "START DATABASE $(DB) WAIT"
	@echo "applying constraints..."
	$(CYPHER) -d $(DB) -f /cypher/constraints.cypher
	@$(MAKE) --no-print-directory stamp
	@$(MAKE) --no-print-directory clean-import
	@echo
	@$(MAKE) --no-print-directory stats

# Record what is loaded, so the next load can see what it would replace. One
# node; deliberately readable by everyone, since "which dataset am I looking
# at" is a fair question for a demo user too.
stamp:
	@txns=$$($(PY) -c "import json;print(json.load(open('out/$(SCALE)/manifest.json'))['row_counts']['transaction'])"); 	seed=$$($(PY) -c "import json;print(json.load(open('out/$(SCALE)/manifest.json'))['reproducibility']['seed'])"); 	$(CYPHER) -d $(DB) "MERGE (m:DatasetManifest {id:'current'}) 	  SET m.preset='$(SCALE)', m.seed=$$seed, m.transactions=$$txns, m.loaded_at=datetime()" >/dev/null
	@echo "stamped $(DB) as preset=$(SCALE)"

# Refuse to replace a larger dataset with a smaller one without FORCE=1.
# The separate-database split above makes this collision unlikely rather than
# impossible: reloading the same preset over a bigger build of itself would
# still lose it silently.
load-guard:
	@test -f out/$(SCALE)/manifest.json || { 		echo "no dataset at out/$(SCALE). Run: make generate SCALE=$(SCALE)"; exit 1; }
	@incoming=$$($(PY) -c "import json;print(json.load(open('out/$(SCALE)/manifest.json'))['row_counts']['transaction'])"); 	existing=$$($(CYPHER) -d $(DB) --format plain 	  "MATCH (m:DatasetManifest) RETURN coalesce(m.transactions,0) AS n" 2>/dev/null 	  | tail -1 | tr -dc '0-9'); 	existing=$${existing:-0}; 	if [ "$${FORCE:-0}" != "1" ] && [ "$$existing" -gt "$$incoming" ]; then 		echo ""; 		echo "REFUSING: $(DB) holds $$existing transactions; this load carries $$incoming."; 		echo "A load replaces the whole store, so the larger dataset would be lost."; 		echo "Re-run with FORCE=1 if that is what you want."; 		echo ""; 		exit 1; 	fi

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

# --- Aura ------------------------------------------------------------------
#
# The mvp graph needs more page cache than a laptop has (PERFORMANCE-NOTES.md),
# so the demo target is a managed instance. `neo4j-admin database upload` wants
# a dump file, and a dump wants the database stopped, so the sequence is
# stop -> dump -> start -> upload. The dump lands in out/import/dumps because
# that is the one host-backed mount the container has; the Docker VM disk does
# not have room for a 28GB store plus its dump.
#
# Aura accepts a dump from this newer server: the mvp store is 2026.07 block
# format and the target instance reports 5.27-aura, and the import service
# upgraded it on the way in.

AURA_DUMP_DIR := out/import/dumps

aura-guard:
	@test -n "$${AURA_URI:-}" || { \
		echo "AURA_URI is not set. Add AURA_URI / AURA_USERNAME / AURA_PASSWORD"; \
		echo "to .env - see .env.example."; exit 1; }

aura-dump: aura-guard
	@mkdir -p $(AURA_DUMP_DIR)
	$(CYPHER) -d system "STOP DATABASE $(DB) WAIT"
	$(COMPOSE) exec -T --user neo4j neo4j neo4j-admin database dump $(DB) \
		--to-path=/import/dumps --overwrite-destination=true
	$(CYPHER) -d system "START DATABASE $(DB) WAIT"
	@ls -lh $(AURA_DUMP_DIR)/$(DB).dump

# Replaces everything in the target instance. Takes ~20 minutes for the mvp
# preset: two minutes of upload, the rest Aura rebuilding the store.
aura-push: aura-dump
	$(COMPOSE) exec -T --user neo4j neo4j sh -c \
		"neo4j-admin database upload $(DB) --from-path=/import/dumps \
		 --to-uri='$$AURA_URI' --to-user='$$AURA_USERNAME' \
		 --to-password='$$AURA_PASSWORD' --overwrite-destination=true"
	@$(MAKE) --no-print-directory aura-setup

# Roles, deny rules and demo users on the Aura instance. Separate from the
# graph because an upload replaces the data, not the security model.
aura-setup: aura-guard
	$(COMPOSE) exec -T --user neo4j neo4j sh -c \
		"cypher-shell -a '$$AURA_URI' -u '$$AURA_USERNAME' -p '$$AURA_PASSWORD' \
		 -d system -f /cypher/aura-setup.cypher"
	@echo "roles applied; demo user is analyst / analystanalyst"

# Time the demo query set. TARGET=local|aura, USER=neo4j|analyst.
bench:
	@bash scripts/bench-demo.sh $${TARGET:-local} $${USER_ROLE:-neo4j}

# --- backup / restore ------------------------------------------------------
#
# Three independent ways back, fastest first:
#
#   1. make restore                        dumps in backups/latest   (minutes)
#   2. make export load SCALE=mvp          Parquet in out/mvp        (~15 min)
#   3. make generate export load SCALE=mvp the seed, byte-identical  (~20 min)
#
# The dump is not redundant with the other two. The dataset is a pure function
# of the seed, but Studio's saved Bloom perspectives, scenes and graphs (the
# `tools-storage` database) are somebody's work and exist nowhere else.
#
# Roles, users and the deny rules are NOT in a dump - they live in `system` -
# and are reapplied from neo4j/nes-setup.cypher, which restore runs.
#
# Defaults are independent of SCALE on purpose. SCALE defaults to `dev`, so a
# bare `make backup` that followed it would quietly dump the empty dev database.
# Pass BACKUP_MAIN=fincrime-dev to back that one up instead.

BACKUP_DIR  ?= backups/latest
BACKUP_MAIN ?= fincrime
BACKUP_DBS  ?= $(BACKUP_MAIN) tools-storage
# ./backups is mounted at /backups in the container (docker-compose.yml).
BACKUP_MNT   = /$(BACKUP_DIR)

backup:
	@mkdir -p $(BACKUP_DIR)
	@studio=$$($(COMPOSE) --profile nes ps --status running -q enterprise-studio 2>/dev/null); \
	fail=0; \
	[ -z "$$studio" ] || { echo "pausing Studio (its asset store is one of the dumps)..."; \
	  $(COMPOSE) --profile nes stop enterprise-studio >/dev/null; }; \
	for db in $(BACKUP_DBS); do \
	  echo "dumping $$db..."; \
	  $(CYPHER) -d system "STOP DATABASE \`$$db\` WAIT" || { fail=1; break; }; \
	  rc=0; \
	  $(COMPOSE) exec -T --user neo4j neo4j neo4j-admin database dump $$db \
	    --to-path=$(BACKUP_MNT) --overwrite-destination=true || rc=$$?; \
	  $(CYPHER) -d system "START DATABASE \`$$db\` WAIT"; \
	  [ $$rc -eq 0 ] || { echo "dump of $$db FAILED"; fail=1; break; }; \
	done; \
	[ -z "$$studio" ] || $(COMPOSE) --profile nes start enterprise-studio >/dev/null; \
	exit $$fail
	@cd $(BACKUP_DIR) && shasum -a 256 *.dump > SHA256SUMS
	@{ \
	  echo "created:       $$(date -u +%FT%TZ)"; \
	  echo "databases:     $(BACKUP_DBS)"; \
	  echo "git:           $$(git rev-parse HEAD)$$(git status --porcelain | grep -q . && echo -dirty)"; \
	  echo "neo4j image:   $$(docker inspect fincrime-neo4j --format '{{.Config.Image}}' 2>/dev/null)"; \
	  echo "studio image:  $$(docker inspect fincrime-nes --format '{{.Config.Image}}' 2>/dev/null)"; \
	  echo "$(BACKUP_MAIN) nodes: $$($(CYPHER) -d $(BACKUP_MAIN) --format plain 'MATCH (n) RETURN count(n)' 2>/dev/null | tail -1)"; \
	  echo "$(BACKUP_MAIN) rels:  $$($(CYPHER) -d $(BACKUP_MAIN) --format plain 'MATCH ()-[r]->() RETURN count(r)' 2>/dev/null | tail -1)"; \
	} > $(BACKUP_DIR)/BACKUP-INFO.txt
	@echo; cat $(BACKUP_DIR)/BACKUP-INFO.txt; echo; du -sh $(BACKUP_DIR)/*.dump

# Rebuild the Docker side from nothing: start Neo4j, recreate databases / roles /
# users from nes-setup.cypher, load every dump, start Studio. Safe to run against
# a freshly cleared volume, which is the case it exists for.
#
# Refuses to overwrite a main database that already holds data (FORCE=1 to
# override), because a restore replaces the whole store and an older dump over a
# newer graph would lose the newer one silently.
restore:
	@test -f $(BACKUP_DIR)/SHA256SUMS || { echo "no backup at $(BACKUP_DIR) (no SHA256SUMS)."; \
	  echo "Other routes: make export load SCALE=mvp  |  make generate export load SCALE=mvp"; exit 1; }
	@echo "verifying checksums..."; cd $(BACKUP_DIR) && shasum -a 256 -c SHA256SUMS
	@$(MAKE) --no-print-directory up
	@echo "applying databases, roles and users..."
	@$(CYPHER) -d system -f /cypher/nes-setup.cypher
	@$(COMPOSE) --profile nes stop enterprise-studio >/dev/null 2>&1 || true
	@nodes=$$($(CYPHER) -d $(BACKUP_MAIN) --format plain "MATCH (n) RETURN count(n) AS n" 2>/dev/null | tail -1 | tr -dc '0-9'); \
	nodes=$${nodes:-0}; \
	if [ "$${FORCE:-0}" != "1" ] && [ "$$nodes" -gt 0 ]; then \
	  echo ""; echo "REFUSING: $(BACKUP_MAIN) already holds $$nodes nodes."; \
	  echo "A restore replaces the whole store. Re-run with FORCE=1 if that is what you want."; \
	  echo ""; exit 1; \
	fi
	@set -e; for db in $(BACKUP_DBS); do \
	  test -f $(BACKUP_DIR)/$$db.dump || { echo "missing $(BACKUP_DIR)/$$db.dump"; exit 1; }; \
	  echo "restoring $$db..."; \
	  $(CYPHER) -d system "STOP DATABASE \`$$db\` WAIT"; \
	  $(COMPOSE) exec -T --user neo4j neo4j neo4j-admin database load $$db \
	    --from-path=$(BACKUP_MNT) --overwrite-destination=true; \
	  $(CYPHER) -d system "START DATABASE \`$$db\` WAIT"; \
	done
	@$(MAKE) --no-print-directory nes
	@echo; echo "restored. counts:"
	@$(CYPHER) -d $(BACKUP_MAIN) --format plain "MATCH (n) RETURN count(n) AS nodes"
	@$(CYPHER) -d $(BACKUP_MAIN) --format plain "MATCH ()-[r]->() RETURN count(r) AS rels"
	@echo; echo "verify with: make release-check SCALE=mvp"

# --- release ---------------------------------------------------------------

VERSION ?= $(shell $(PY) -c "import fincrime; print(fincrime.__version__)")

# The M6 payload. Dumps the loaded graph first, because a release without the
# graph form is only half of what a recipient needs; the dump goes to the one
# host-backed mount the container has.
release: release-check
	@mkdir -p $(AURA_DUMP_DIR)
	$(CYPHER) -d system "STOP DATABASE $(DB) WAIT"
	$(COMPOSE) exec -T --user neo4j neo4j neo4j-admin database dump $(DB) \
		--to-path=/import/dumps --overwrite-destination=true
	$(CYPHER) -d system "START DATABASE $(DB) WAIT"
	uv run fincrime release --scale $(SCALE) --version $(VERSION) \
		--dump $(AURA_DUMP_DIR)/$(DB).dump
	@echo
	@echo "Payload is large and gitignored except its docs and manifest:"
	@du -sh releases/$(VERSION)

# The M6 exit criterion: the whole demo set runs as the unprivileged role and
# returns rows, and the answer key stays unreadable. Gates the release, so a
# payload cannot be cut from a graph whose RBAC is broken.
release-check:
	@$(MAKE) --no-print-directory rbac-check
	@GATE=1 bash scripts/bench-demo.sh local analyst

aura-bench:
	@bash scripts/bench-demo.sh aura neo4j
	@bash scripts/bench-demo.sh aura analyst

databases:
	@$(CYPHER) -d system --format plain \
		"SHOW DATABASES YIELD name, currentStatus WHERE name STARTS WITH 'fincrime'"
	@for db in fincrime fincrime-dev; do \
		$(CYPHER) -d $$db --format plain \
		  "MATCH (m:DatasetManifest) RETURN '$$db' AS db, m.preset AS preset, \
		   m.transactions AS txns, m.loaded_at AS loaded" 2>/dev/null | tail -n +2; \
	done

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
