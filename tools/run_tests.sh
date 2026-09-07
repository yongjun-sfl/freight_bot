#!/bin/bash
# Run the test suite through docker-compose in BACKGROUND, capture the result to
# a file, and wait ONCE (blocking wait) -- sidestepping the Cline 30s tool
# timeout. Never rapid-poll.
#
# Usage:
#   tools/run_tests.sh                 # full suite
#   tools/run_tests.sh -k 'reposted'   # pass any pytest args through
#   tools/run_tests.sh -f test_state_machine   # custom filter
#
# The container keeps its `--name` so the waiter can find it; result goes to
# backups/.test_result.txt (gitignored dot-file), like the replay waiter.

set -u

cd "$(dirname "$0")/.." || exit 1
OUT="backups/.test_result.txt"
PASSED=()
[ $# -ge 2 ] && [ "$1" = "-k" ] && { PASSED=("-k" "$2"); shift 2; }
[ $# -ge 2 ] && [ "$1" = "-f" ] && { PASSED=("$2"); shift 2; }
if [ $# -ge 1 ]; then PASSED=("$@"); fi

# Build a shell-quoted pytest-args string so filters like
#   -k 'reposted or stale_bol'
# survive as ONE argument inside the container's `sh -c` command.
PYTEST_ARGS=""
for a in "${PASSED[@]}"; do
  PYTEST_ARGS="$PYTEST_ARGS $(printf '%q' "$a")"
done

# Pick one short, unique --name per invocation so the docker run name cannot
# collide with a still-running container of a previous run.
CID="test_run_$$"
LAUNCH_LOG="/tmp/${CID}_launch.log"

echo "▶ Launching pytest ${PASSED[*]:-...} in background (container=$CID)…"
docker compose -f docker-compose.yml -f docker-compose.test.yml run \
  --name "$CID" tests \
  sh -c "pip install --no-cache-dir -q -r requirements-dev.txt && \
         python -m pytest tests/test_state_machine.py ${PYTEST_ARGS:-} -q " \
  > "$LAUNCH_LOG" 2>&1 &
echo "  launch pid=$!"

# The compose build for the base image can take a while before the container is
# even Created. Wait ONE bounded loop for it to exist, then continue onto the
# running-wait below. This avoids the waiter racing ahead during the build.
for _ in $(seq 1 60); do
  [ -n "$(docker inspect -f '{{.Id}}' "$CID" 2>/dev/null)" ] && break
  sleep 8
done

# Wait ONCE with `docker wait` (container stays after exit because --rm is not
# used), then read the exit code and logs before removing it.
if docker inspect -f '{{.State.Running}}' "$CID" >/dev/null 2>&1; then
  RC=$(docker wait "$CID")
else
  RC="?"
fi

{
  echo "=== TESTS $(date) ==="
  echo "rc=$RC"
  echo "--- pytest output ---"
  if docker logs "$CID" >/dev/null 2>&1; then
    docker logs "$CID" 2>&1 | tail -n 60
  else
    tail -n 60 "$LAUNCH_LOG"
  fi
} > "$OUT" 2>&1

docker rm -f "$CID" >/dev/null 2>&1
echo "done $(date)" >> "$OUT"
echo "▶ Done — see $OUT"
