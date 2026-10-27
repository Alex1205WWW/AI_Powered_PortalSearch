#!/bin/sh
# Manual backup from the host (the worker also runs this nightly).
set -e
cd "$(dirname "$0")/.."
docker compose exec worker python -c "from app.worker.jobs import backup; print(backup())"
