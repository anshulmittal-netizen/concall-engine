#!/usr/bin/env bash
# Commit the announcement tracker without losing another run's work: merge data/ann_IN.json with the latest remote
# copy (union of filings; ours wins on the same id unless only theirs has the Claude read), rebuild the dashboard
# file data/ann_in.json from the merged store, then commit both on top of origin/main.
set -e
git config user.name "concall-engine"
git config user.email "concall-engine@users.noreply.github.com"
for i in 1 2 3 4; do
  git fetch -q origin main
  git show origin/main:data/ann_IN.json > /tmp/ann_up.json 2>/dev/null || echo '{"items":{}}' > /tmp/ann_up.json
  python - <<'PY'
import json
import ann
from common import DATA, read_json, write_json
up = json.load(open("/tmp/ann_up.json"))
ours = read_json(ann.PATH, {"items": {}})
items = dict(up.get("items", {}))
for k, v in ours.get("items", {}).items():
    u = items.get(k)
    items[k] = u if (u and u.get("ai") and not v.get("ai")) else v
store = {**up, **ours, "items": items}
store["checked"] = max(filter(None, [up.get("checked"), ours.get("checked")]), default=None)
write_json(ann.PATH, store, compact=True)
comps = read_json(DATA / "universe_IN.json", {}).get("companies", {})
keep = int(ann.settings().get("announcements", {}).get("keep_days", 90))
write_json(ann.OUT, ann.build(store, comps, ann.revenue_map(), keep), compact=True)
PY
  git reset -q origin/main   # index = remote; only the merged files are staged
  git add data/ann_IN.json data/ann_in.json
  git diff --cached --quiet && exit 0
  git commit -q -m "Announcements $(date -u +'%F %H:%M')"
  git push -q origin HEAD:main && exit 0
  sleep 5
done
exit 1
