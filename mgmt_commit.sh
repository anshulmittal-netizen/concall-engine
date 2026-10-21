#!/usr/bin/env bash
# Commit data/mgmt_IN.json without ever losing another run's filings: merge with the latest
# remote copy (union of items; ours wins on the same id), then commit on top of it.
set -e
git config user.name "concall-engine"
git config user.email "concall-engine@users.noreply.github.com"
for i in 1 2 3 4; do
  git fetch -q origin main
  git show origin/main:data/mgmt_IN.json > /tmp/up.json 2>/dev/null || echo '{"items":{}}' > /tmp/up.json
  python - <<'PY'
import json
up = json.load(open("/tmp/up.json"))
try:
    ours = json.load(open("data/mgmt_IN.json"))
except FileNotFoundError:
    ours = {"items": {}}
items = {**up.get("items", {}), **ours.get("items", {})}
out = {**up, **ours, "items": items}
out["checked"] = max(filter(None, [up.get("checked"), ours.get("checked")]), default=None)
json.dump(out, open("data/mgmt_IN.json", "w"), ensure_ascii=False, indent=1)
PY
  git reset -q origin/main   # index = remote; only the merged file is staged
  git add data/mgmt_IN.json
  git diff --cached --quiet && exit 0
  git commit -q -m "Management changes $(date -u +'%F %H:%M')"
  git push -q origin HEAD:main && exit 0
  sleep 5
done
exit 1
