#!/bin/bash
cd "$(dirname "$0")/.."
wait_run() { # id
  while :; do
    st=$(gh run view "$1" --json status -q '.status' 2>/dev/null)
    [ "$st" != "in_progress" ] && [ "$st" != "queued" ] && [ -n "$st" ] && break
    sleep 45
  done
  echo "RUN $1 status=$st conclusion=$(gh run view "$1" --json conclusion -q '.conclusion' 2>/dev/null)"
}
echo "=== waiting for DIAG run 44021467417 ==="
wait_run 44021467417
rm -rf .ci_logs/diagart
gh run download 44021467417 -D .ci_logs/diagart 2>&1 | tail -2
echo "=== artifact dirs ==="; find .ci_logs/diagart -maxdepth 3 -type d 2>/dev/null | tail -5
echo "=== DRC report files ==="; find .ci_logs/diagart \( -iname "*drc*" -o -iname "*.drc" -o -iname "*violation*" \) 2>/dev/null | head -20
echo "=== drt step logs ==="; find .ci_logs/diagart -path "*detailedrouting*" -type f 2>/dev/null | head -10
echo "=== violation summary from DRT log ==="
for f in $(find .ci_logs/diagart -name "*.log" -path "*routing*" 2>/dev/null | head -5); do
  echo "--- $f"; grep -E "Number of violations|Viol/Layer|Short|Spacing|Total wire length" "$f" | head -20
done
echo "=== waiting for ATTEMPT 8 run 44021116495 ==="
wait_run 44021116495
echo "=== attempt8 key lines ==="
timeout 300 gh run view 44021116495 --log 2>/dev/null > .ci_logs/run8_full.log
grep -E "DRT-0199|Chip area|Effective utilization|Utilization:|ERROR|violation|precheck|gl_test|WNS|TNS" .ci_logs/run8_full.log | tail -40
