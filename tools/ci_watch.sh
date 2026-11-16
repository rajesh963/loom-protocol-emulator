#!/usr/bin/env bash
# Wait for the newest gds + test workflow runs, then summarise.
cd /home/rajesh/Documents/deepseek-harness/default-workspace/chipchallenge/loom
GDS=$(gh run list --workflow=gds.yaml --limit 1 --json databaseId -q '.[0].databaseId')
TST=$(gh run list --workflow=test.yaml --limit 1 --json databaseId -q '.[0].databaseId')
echo "watching gds=$GDS test=$TST" > CI_RESULTS.md
deadline=$((SECONDS + 3000))
while [ $SECONDS -lt $deadline ]; do
  g=$(gh run view "$GDS" --json status,conclusion -q '.status+" "+(.conclusion//"")' 2>/dev/null)
  t=$(gh run view "$TST" --json status,conclusion -q '.status+" "+(.conclusion//"")' 2>/dev/null)
  echo "[$SECONDS] gds=$g test=$t" >> CI_RESULTS.md
  case "$g" in completed*) gd=1;; *) gd=0;; esac
  case "$t" in completed*) td=1;; *) td=0;; esac
  [ "$gd" = 1 ] && [ "$td" = 1 ] && break
  sleep 45
done
{
  echo; echo "## FINAL"; echo "gds: $(gh run view "$GDS" --json status,conclusion -q '.status+" "+(.conclusion//"")')"
  echo "test: $(gh run view "$TST" --json status,conclusion -q '.status+" "+(.conclusion//"")')"
  echo; echo "## TEST JOB SUMMARY"; gh run view "$TST" --log 2>/dev/null | grep -E "TESTS=|PASS=|FAIL=" | tail -3
  echo; echo "## GDS KEY METRICS"; gh run view "$GDS" --log 2>/dev/null | grep -iE "utilization|wns|tns|slack|die area|cell area|Total cell|precheck|DRC|LVS|violation|error:" | tail -60
  echo; echo "## GDS JOBS"; gh run view "$GDS" --json jobs -q '.jobs[]|.name+" "+.status+" "+(.conclusion//"")'
} >> CI_RESULTS.md 2>&1
echo DONE >> CI_RESULTS.md
