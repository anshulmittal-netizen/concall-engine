#!/usr/bin/env bash
# Commit the results files on top of the latest main. Only the 30-minute watch writes them and its runs
# never overlap (one concurrency group), so this run's copy is always the newest.
set -e
git config user.name "concall-engine"
git config user.email "concall-engine@users.noreply.github.com"
for i in 1 2 3 4; do
  git fetch -q origin main
  git reset -q origin/main   # index = remote; only the results files are staged
  git add data/results_in.json data/results_state_IN.json
  git diff --cached --quiet && exit 0
  git commit -q -m "Results $(date -u +'%F %H:%M')"
  git push -q origin HEAD:main && exit 0
  sleep 5
done
exit 1
