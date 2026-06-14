#!/usr/bin/env bash
# jq_tail.sh — tail structurat pentru logs/apex_structured.jsonl
# Necesita: jq (apt install jq / brew install jq)
#
# Folosire:
#   ./scripts/jq_tail.sh             # toate logurile, pretty
#   ./scripts/jq_tail.sh entries     # doar ENTRY_LONG / ENTRY_SHORT
#   ./scripts/jq_tail.sh exits       # TP1/2/3/SL/TIMEOUT
#   ./scripts/jq_tail.sh errors      # WARNING + ERROR
#   ./scripts/jq_tail.sh pnl         # linii cu camp pnl
#   ./scripts/jq_tail.sh scores      # entries cu score
#   ./scripts/jq_tail.sh regime      # schimbari regim
#   ./scripts/jq_tail.sh api         # API_ERROR

LOG="logs/apex_structured.jsonl"

if [ ! -f "$LOG" ]; then
  echo "Fisierul $LOG nu exista inca. Porneste botul mai intai."
  exit 1
fi

FILTER="."
case "${1:-all}" in
  entries) FILTER='select(.event == "ENTRY_LONG" or .event == "ENTRY_SHORT")' ;;
  exits)   FILTER='select(.event | test("TP[123]_HIT|SL_HIT|TIMEOUT|POSITION_CLOSED"))' ;;
  errors)  FILTER='select(.level == "ERROR" or .level == "WARNING")' ;;
  pnl)     FILTER='select(.pnl != null) | {time, event, side, price, pnl, score}' ;;
  scores)  FILTER='select(.score != null) | {time, event, score, regime, rsi, side}' ;;
  regime)  FILTER='select(.event == "REGIME_CHANGE" or .event == "MTF_REFRESH")' ;;
  api)     FILTER='select(.event == "API_ERROR")' ;;
  all)     FILTER='.' ;;
  *)       echo "Filtru necunoscut: $1"; exit 1 ;;
esac

echo "Urmaresc $LOG | filtru: ${1:-all}"
echo "---"
tail -f "$LOG" | jq --unbuffered "$FILTER"
