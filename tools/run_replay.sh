#!/bin/bash
# Replay a real day through docker-compose in BACKGROUND, capture the score to a
# file, and wait ONCE (blocking wait) -- sidestepping the Cline 30s tool
# timeout, per the "no rapid-poll" operating rule in .memory.md.
#
# Usage:
#   tools/run_replay.sh                 # text-only, default sheet
#   REPLAY_PHOTOS=1  tools/run_replay.sh # vision pass (reads BOL/receiver PODs)
#   REPLAY_DRIVER="JOHN SHIM" tools/run_replay.sh
#   REPLAY_DEBUG=1   tools/run_replay.sh # why each missed row was rejected
#
# Result goes to backups/.replayN_result.txt. The scratch DB `replay_scratch`
# stays on mysql_db for inspection.

set -u
cd "$(dirname "$0")/.." || exit 1

# Next numbered result file, mirroring .replay4/.replay5.
N=4
for f in backups/.replay*_result.txt; do
  n="${f##*.replay}"; n="${n%%_*}"
  [ -n "$n" ] && [ "$n" -gt "$N" ] 2>/dev/null && N=$n
done
OUT="backups/.replay$((N+1))_result.txt"
CID="replay_run_$$"
LAUNCH_LOG="/tmp/${CID}_launch.log"

echo "▶ Launching replay in background (container=$CID)…"
docker compose -f docker-compose.yml -f docker-compose.test.yml run \
  --name "$CID" replay > "$LAUNCH_LOG" 2>&1 &
echo "  launch pid=$!"

# The compose build can take a while before the container even exists. Wait ONE
# bounded loop for it to appear, then wait for it to exit -- so the waiter never
# races ahead during the build phase.
for _ in $(seq 1 60); do
  [ -n "$(docker inspect -f '{{.Id}}' "$CID" 2>/dev/null)" ] && break
  sleep 8
done

# Wait ONCE with `docker wait` for the replay to finish (container stays after
# exit because --rm is not used), then read the logs before removing it.
if docker inspect -f '{{.State.Running}}' "$CID" >/dev/null 2>&1; then
  RC=$(docker wait "$CID")
else
  RC="?"
fi

{
  echo "=== REPLAY $OUT $(date) ==="
  echo "rc=$RC"
  echo "--- per-driver table + missing/extra ---"
  if docker logs "$CID" >/dev/null 2>&1; then
    docker logs "$CID" 2>&1 |
      grep -vE '^\[notice\]|^$|pip|WARNING: Running|Warning:|aiomysql|cursors' |
      tail -n 300
  else
    grep -vE '^\[notice\]|^$|pip|WARNING: Running|Warning:|aiomysql|cursors' "$LAUNCH_LOG" |
      tail -n 300
  fi
  echo "--- legs left in replay_scratch ---"
  echo "  docker compose exec mysql_db mysql -uroot -p -D replay_scratch"
  echo "  SELECT * FROM shuttle_legs ORDER BY user_id, departure_time;"
  echo "=== END ==="
} > "$OUT" 2>&1

docker rm -f "$CID" >/dev/null 2>&1
echo "done $(date)" >> "$OUT"
echo "▶ Done — see $OUT"
