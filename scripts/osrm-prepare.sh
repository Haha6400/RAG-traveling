#!/usr/bin/env bash
# Download an OSM extract and preprocess it for OSRM (MLD pipeline, car profile).
# Usage: ./scripts/osrm-prepare.sh [PBF_URL]
# Pick a small region for development; a whole country can take a while and several GB of RAM.
set -euo pipefail

PBF_URL="${1:-https://download.geofabrik.de/asia/vietnam-latest.osm.pbf}"
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DATA_DIR="$ROOT_DIR/data/osrm"
IMAGE="ghcr.io/project-osrm/osrm-backend:v5.27.1"

mkdir -p "$DATA_DIR"
echo "Downloading $PBF_URL"
curl -fL -o "$DATA_DIR/region.osm.pbf" "$PBF_URL"

run() { docker run --rm -t -v "$DATA_DIR:/data" "$IMAGE" "$@"; }
run osrm-extract -p /opt/car.lua /data/region.osm.pbf
run osrm-partition /data/region.osrm
run osrm-customize /data/region.osrm

echo "Done. Start OSRM with: docker compose --profile geo up osrm"
