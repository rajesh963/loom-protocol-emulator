#!/usr/bin/env bash
cd /home/rajesh/Documents/deepseek-harness/default-workspace/chipchallenge/loom
SHA=$(git rev-parse HEAD)
echo "watching gds for $SHA" > CI_RESULTS4.md
for i in $(seq 1 60); do
  ID=$(gh run list --workflow=gds.yaml --limit 5 --json databaseId,headSha,status -q ".[] | select(.headSha==\"$SHA\") | .databaseId" 2>/dev/null | head -1)
  [ -n "$ID" ] && break
  sleep 10
done
echo "run id: $ID" >> CI_RESULTS4.md
while :; do
  ST=$(gh run view "$ID" --json status,conclusion -q '.status+" "+(.conclusion//"")' 2>/dev/null)
  echo "[$(date +%H:%M:%S)] $ST" >> CI_RESULTS4.md
  case "$ST" in completed*) break;; esac
  sleep 45
done
{
  echo; echo "## CONCLUSION"; gh run view "$ID" --json conclusion,status -q '.status+" "+.conclusion' 2>/dev/null
  echo; echo "## JOBS"; gh run view "$ID" --json jobs -q '.jobs[]|.name+" "+.status+" "+(.conclusion//"")' 2>/dev/null
  echo; echo "## AREA / UTIL / ERRORS"
  gh run view "$ID" --log 2>/dev/null | grep -iE "Chip area for module|Effective utilization|Utilization:|utilization__stdcell|error|failed|DPL-|GPL-|violation|precheck|WNS|TNS|slack" | grep -viE "buf_1|warning: no" | tail -40
} >> CI_RESULTS4.md 2>&1
echo DONE >> CI_RESULTS4.md
