#!/bin/bash
# usage: tools/watch_run.sh <run-id> <logfile>
id="$1"; out="$2"; : > "$out"
while :; do
  st=$(gh run view "$id" --json status,conclusion -q '.status' 2>/dev/null)
  ts=$(date -u +%H:%M:%S)
  echo "[$ts] status=$st" >> "$out"
  [ "$st" != "in_progress" ] && [ "$st" != "queued" ] && [ -n "$st" ] && break
  sleep 45
done
echo "RUN $id FINISHED status=$st" >> "$out"
gh run view "$id" --json conclusion -q '.conclusion' >> "$out" 2>&1
