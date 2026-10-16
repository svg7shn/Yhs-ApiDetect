#!/usr/bin/env bash
# bench.sh — collect official Anthropic API baselines for the latest Claude
# models. Output is the "ground truth" reference our detectors should see on a
# real, unaltered API; subsequent relay tests will be diffed against these.
#
# Usage (run on the deploy host):
#   OFFICIAL_KEY=sk-ant-...  ./bench.sh
#   OFFICIAL_KEY=sk-ant-...  ./bench.sh -o /opt/baselines
#   OFFICIAL_KEY=sk-ant-...  ./bench.sh --out ~/baselines --mode standard
#
# All options also accept env vars: OUT / MODE / MODELS / OFFICIAL_BASE.
#
# After running, fetch results back to the project tree:
#   scp -r <host>:<OUT>/*.json relay-detector/data/baselines/

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
DETECT="$HERE/venv/bin/relay-detector"
PY="$HERE/venv/bin/python"

OUT="${OUT:-/tmp/baselines}"
MODE="${MODE:-full}"
MODELS="${MODELS:-claude-opus-4-7 claude-sonnet-4-6 claude-haiku-4-5 claude-opus-4-6}"
OFFICIAL_BASE="${OFFICIAL_BASE:-https://www.yhshu.ai/}"

print_usage() {
  cat <<EOF
Usage: $(basename "$0") [options]

Collect official Anthropic API baselines for the latest Claude models.

Options:
  -o, --out PATH         Output directory (default: $OUT)
  -m, --mode MODE        quick / standard / full (default: $MODE)
      --models "..."     Space-separated model list
                         (default: $MODELS)
      --base URL         Official API base URL
                         (default: $OFFICIAL_BASE)
  -h, --help             Show this help and exit

Required env var:
  OFFICIAL_KEY           sk-ant-... (also accepted: ANTHROPIC_KEY)

Examples:
  OFFICIAL_KEY=sk-ant-... ./bench.sh
  OFFICIAL_KEY=sk-ant-... ./bench.sh -o /opt/baselines
  OFFICIAL_KEY=sk-ant-... ./bench.sh --out ~/baselines --mode quick
  OFFICIAL_KEY=sk-ant-... ./bench.sh --models "claude-opus-4-7"
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -o|--out)    OUT="$2"; shift 2 ;;
    -m|--mode)   MODE="$2"; shift 2 ;;
    --models)    MODELS="$2"; shift 2 ;;
    --base)      OFFICIAL_BASE="$2"; shift 2 ;;
    -h|--help)   print_usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; print_usage >&2; exit 2 ;;
  esac
done

OFFICIAL_KEY="${OFFICIAL_KEY:-${ANTHROPIC_KEY:-}}"
if [[ -z "$OFFICIAL_KEY" ]]; then
  echo "✗ OFFICIAL_KEY env var is required (sk-ant-...)" >&2
  echo >&2
  print_usage >&2
  exit 2
fi
if [[ ! -x "$DETECT" ]]; then
  echo "✗ $DETECT not found — run ./deploy.sh first or build the venv." >&2
  exit 1
fi

# Warn if the chosen path lives under the deploy root (deploy.sh uses
# `rsync --delete`, which would wipe baselines on the next deploy).
case "$OUT" in
  "$HERE"/*|"$HERE")
    echo "⚠  warning: $OUT is under the project deploy path." >&2
    echo "   ./deploy.sh will delete files here on next sync." >&2
    echo "   consider --out /tmp/baselines or another location outside $HERE." >&2
    echo >&2
    ;;
esac

mkdir -p "$OUT"

echo "Collecting Anthropic baselines:"
echo "  api    $OFFICIAL_BASE"
echo "  out    $OUT"
echo "  mode   $MODE"
echo "  models $MODELS"
echo

i=0
for m in $MODELS; do
  i=$((i + 1))
  out="$OUT/${m}_${MODE}.json"
  echo "[$i] $m"
  if "$DETECT" detect \
        --base-url "$OFFICIAL_BASE" \
        --api-key "$OFFICIAL_KEY" \
        --model "$m" \
        --mode "$MODE" \
        --output "$out" >/dev/null 2>"$out.log"; then
    score=$("$PY" -c \
      "import json,sys; d=json.load(open(sys.argv[1])); \
print(f\"{d['total_score']:.1f}  {d['summary']}  req={d['performance']['request_count']}  in={d['performance']['usage']['input_tokens']}  out={d['performance']['usage']['output_tokens']}\")" \
      "$out")
    rm -f "$out.log"
    echo "    $score"
    echo "    → $out"
  else
    echo "    ✗ failed (see $out.log)"
  fi
  echo
done

echo "✓ done — $(ls "$OUT"/*.json 2>/dev/null | wc -l | tr -d ' ') file(s) in $OUT/"
ls -lh "$OUT"/*.json 2>/dev/null || true
echo
echo "Next: from your local machine,"
echo "  scp -r <host>:$OUT/*.json relay-detector/data/baselines/"
