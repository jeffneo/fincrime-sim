#!/usr/bin/env bash
# Time every query in neo4j/demo/ against one target, as one user.
#
#   scripts/bench-demo.sh local  neo4j            # self-hosted, admin
#   scripts/bench-demo.sh local  analyst          # self-hosted, demo role
#   scripts/bench-demo.sh aura   neo4j            # Aura, admin
#   scripts/bench-demo.sh aura   analyst          # Aura, demo role
#
# cypher-shell runs inside the fincrime-neo4j container in both cases - it is
# the only place a matching client is installed. For `aura` it just dials out.
#
# A query that exceeds TIMEOUT is abandoned AND terminated server-side. That
# second part is load-bearing: killing the client leaves the server executing,
# and one abandoned query blocks everything measured after it
# (PERFORMANCE-NOTES.md).
set -uo pipefail

cd "$(dirname "$0")/.."
set -a
# shellcheck disable=SC1091
. ./.env
set +a

TARGET="${1:-local}"
USERNAME="${2:-neo4j}"
TIMEOUT="${TIMEOUT:-900}"
# An account with real activity. The first account in the id space is a
# dormant savings account, and the drill-down returns nothing for it.
ACCOUNT_ID="${ACCOUNT_ID:-ACC-000043670}"

# The demo queries are window-scoped. The mvp preset covers calendar 2025; one
# month is the unit an analyst actually works in, and the unscoped year does
# not return at this scale on any hardware tried so far.
#
# October rather than an arbitrary month: mule rings run for one to three
# months each, spread across the year, so a window has to CONTAIN one for
# 03_mule_shared_device to return anything. Three of them overlap October at
# the shipped seed. An admin can list them - see the release README.
WINDOW_START="${WINDOW_START:-2025-10-01T00:00:00Z}"
WINDOW_END="${WINDOW_END:-2025-11-01T00:00:00Z}"

# With --gate, exit non-zero if any query errors, times out, or returns no
# rows. That is the M6 demo-role walkthrough: the whole set has to work as the
# unprivileged role, not merely not crash.
GATE="${GATE:-0}"
failures=0

case "$TARGET" in
  local)
    ADDR="neo4j://localhost:7687"
    DATABASE="${DB:-fincrime}"
    case "$USERNAME" in
      neo4j)   PASSWORD="$NEO4J_PASSWORD" ;;
      analyst) PASSWORD="analystanalyst" ;;
      scorer)  PASSWORD="scorerscorer" ;;
      *) echo "unknown user $USERNAME" >&2; exit 1 ;;
    esac
    ;;
  aura)
    ADDR="$AURA_URI"
    DATABASE="neo4j"
    case "$USERNAME" in
      neo4j)   PASSWORD="$AURA_PASSWORD" ;;
      analyst) PASSWORD="analystanalyst" ;;
      scorer)  PASSWORD="scorerscorer" ;;
      *) echo "unknown user $USERNAME" >&2; exit 1 ;;
    esac
    ;;
  *) echo "usage: $0 {local|aura} [neo4j|analyst|scorer]" >&2; exit 1 ;;
esac

# macOS ships neither `timeout` nor `gtimeout` unless coreutils is installed,
# so the cap is a watchdog. perl's alarm was tried first and does not work
# here: the timer is lost across exec into the docker client, and the query
# runs unbounded.
#
# Killing the client does NOT stop the query - that is what reap() is for.
# The docker client does not report the signal in its exit code, so the
# watchdog leaves a flag file instead of relying on 143.
TIMED_OUT_FLAG=$(mktemp)
run_capped() {
  rm -f "$TIMED_OUT_FLAG"
  "$@" &
  local job=$!
  ( sleep "$TIMEOUT"; kill -TERM "$job" 2>/dev/null && : >"$TIMED_OUT_FLAG" ) &
  local watchdog=$!
  local code=0
  wait "$job" || code=$?
  kill "$watchdog" 2>/dev/null
  wait "$watchdog" 2>/dev/null
  return "$code"
}

# Terminate anything this harness abandoned, so the next timing is clean.
reap() {
  local ids
  ids=$(docker compose exec -T --user neo4j neo4j \
    cypher-shell -a "$ADDR" -u "$1" -p "$2" -d "$DATABASE" --format plain \
    "SHOW TRANSACTIONS YIELD transactionId, username, elapsedTime
     WHERE username = '$USERNAME' AND elapsedTime.seconds > 5
     RETURN transactionId" 2>/dev/null \
    | grep -oE '[A-Za-z0-9_-]+-transaction-[0-9]+' | tr '\n' ' ')
  for id in $ids; do
    [ -z "$id" ] && continue
    docker compose exec -T --user neo4j neo4j \
      cypher-shell -a "$ADDR" -u "$1" -p "$2" -d "$DATABASE" \
      "TERMINATE TRANSACTION '$id'" >/dev/null 2>&1
    echo "    (terminated leftover $id)"
  done
}

printf '\n%s as %s, database %s, timeout %ss\n' "$TARGET" "$USERNAME" "$DATABASE" "$TIMEOUT"
printf '%-34s %10s  %s\n' QUERY SECONDS RESULT
printf '%.0s-' {1..70}; printf '\n'

for f in neo4j/demo/*.cypher; do
  name=$(basename "$f" .cypher)
  out=$(mktemp)
  start=$(date +%s)
  if run_capped docker compose exec -T --user neo4j neo4j \
      cypher-shell -a "$ADDR" -u "$USERNAME" -p "$PASSWORD" -d "$DATABASE" --format plain \
      -P "account_id => '$ACCOUNT_ID'" \
      -P "window_start => '$WINDOW_START'" \
      -P "window_end => '$WINDOW_END'" \
      -f "/cypher/demo/$name.cypher" \
      >"$out" 2>&1; then
    elapsed=$(( $(date +%s) - start ))
    # This build's cypher-shell prints JVM Unsafe warnings on some
    # invocations; they would otherwise be counted as result rows.
    rows=$(( $(grep -cvE '^(WARNING|$)' "$out") - 1 ))
    [ "$rows" -lt 0 ] && rows=0
    printf '%-34s %10s  %s rows\n' "$name" "$elapsed" "$rows"
    if [ "$rows" -eq 0 ]; then failures=$((failures + 1)); fi
  else
    code=$?
    elapsed=$(( $(date +%s) - start ))
    failures=$((failures + 1))
    if [ -f "$TIMED_OUT_FLAG" ] || [ "$code" -eq 143 ] || [ "$code" -eq 124 ]; then
      printf '%-34s %10s  TIMEOUT\n' "$name" "$elapsed"
    else
      printf '%-34s %10s  ERROR: %s\n' "$name" "$elapsed" "$(tail -2 "$out" | tr '\n' ' ' | cut -c1-160)"
    fi
  fi
  rm -f "$out"
  # Reap as an admin: the demo role cannot see other users' transactions, and
  # cannot terminate its own once the client is gone.
  if [ "$TARGET" = "aura" ]; then reap neo4j "$AURA_PASSWORD"; else reap neo4j "$NEO4J_PASSWORD"; fi
done
printf '\n'

if [ "$GATE" = "1" ] && [ "$failures" -gt 0 ]; then
  echo "$failures of the demo queries returned nothing or failed, as $USERNAME."
  echo "For 03_mule_shared_device, check the window contains a mule ring."
  exit 1
fi
