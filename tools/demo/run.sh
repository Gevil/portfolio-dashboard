#!/usr/bin/env bash
# Run an isolated DEMO instance (fictional data, demo credentials, port 8699) and capture docs media.
# Never touches your real config/, data/ or results/.
#
#   bash tools/demo/run.sh up        # seed + start the demo container, wait for /health
#   bash tools/demo/run.sh capture   # screenshots + walkthrough video into $MEDIA_DIR/raw
#   bash tools/demo/run.sh down      # stop and remove the demo container and $DEMO_DIR
#
# Env: DEMO_DIR (default /tmp/pd-demo), MEDIA_DIR (default /tmp/pd-media), IMAGE (default
# localhost/portfolio-dashboard:latest), LANES_DIR (optional dir with lanes.conf to mount at /app/lanes),
# PLAYWRIGHT_PY (python with playwright installed, default: python3).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEMO_DIR="${DEMO_DIR:-/tmp/pd-demo}"
MEDIA_DIR="${MEDIA_DIR:-/tmp/pd-media}"
IMAGE="${IMAGE:-localhost/portfolio-dashboard:latest}"
PY="${PLAYWRIGHT_PY:-python3}"
NAME=portfolio-dashboard-demo
DEMO_CRED="demo:demo-pass"   # fictional, defined in demo.env.txt
PORT=8699

case "${1:-}" in
up)
  mkdir -p "$DEMO_DIR/config" "$DEMO_DIR/data" "$DEMO_DIR/results"
  cp "$HERE/demo-config.json" "$DEMO_DIR/config/config.json"
  cp "$HERE/demo.env.txt" "$DEMO_DIR/env"
  DEMO_DIR="$DEMO_DIR" python3 "$HERE/seed.py"
  podman rm -f "$NAME" >/dev/null 2>&1 || true
  lanes=()
  if [ -n "${LANES_DIR:-}" ]; then
    lanes=(-v "$LANES_DIR:/app/lanes:ro,z" -e LANES_CONF=/app/lanes/lanes.conf -e LANES_ENRICH=/app/lanes/lanes-enrich.json)
  fi
  podman run -d --name "$NAME" -p "127.0.0.1:$PORT:8601" \
    -v "$DEMO_DIR/config:/app/config:z" -v "$DEMO_DIR/data:/app/data:z" -v "$DEMO_DIR/results:/app/results:z" \
    "${lanes[@]}" --env-file "$DEMO_DIR/env" -e CONFIG_PATH=/app/config/config.json "$IMAGE" >/dev/null
  for _ in $(seq 1 60); do
    [ "$(curl -s -o /dev/null -w '%{http_code}' -u "$DEMO_CRED" "http://127.0.0.1:$PORT/health")" = 200 ] && { echo "demo up: http://127.0.0.1:$PORT (demo / demo-pass)"; exit 0; }
    sleep 3
  done
  echo "demo did not become healthy" >&2; exit 1 ;;
capture)
  MEDIA_DIR="$MEDIA_DIR" "$PY" -u "$HERE/capture.py"
  MEDIA_DIR="$MEDIA_DIR" "$PY" -u "$HERE/capture_detail.py" ;;
down)
  podman rm -f "$NAME" >/dev/null 2>&1 || true
  rm -rf "$DEMO_DIR"
  echo "demo removed" ;;
*) sed -n '2,11p' "${BASH_SOURCE[0]}"; exit 2 ;;
esac
