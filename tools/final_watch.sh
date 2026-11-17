#!/bin/bash
cd "$(dirname "$0")/.."
id=44047427913
while :; do
  st=$(gh run view $id --json status -q '.status' 2>/dev/null)
  [ "$st" != "in_progress" ] && [ "$st" != "queued" ] && [ -n "$st" ] && break
  sleep 60
done
echo "FINAL RUN $id status=$st conclusion=$(gh run view $id --json conclusion -q '.conclusion' 2>/dev/null)"
gh run view $id --json jobs -q '.jobs[]|.name+" "+.status+" "+(.conclusion//"-")'
timeout 300 gh run view $id --log 2>/dev/null > .ci_logs/run_final.log
echo "=== GL result ==="; grep -E "GL test.*TESTS=" .ci_logs/run_final.log | tail -2
echo "=== GDS area/util/timing ==="; grep -E "Chip area for module|Effective utilization|Utilization: " .ci_logs/run_final.log | tail -4
echo "=== errors ==="; grep -E "ERROR|##\[error\]" .ci_logs/run_final.log | head -10
echo "=== precheck ==="; grep -E "precheck.*(success|completed)|Total area of the design" .ci_logs/run_final.log | tail -5
