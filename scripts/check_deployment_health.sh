#!/usr/bin/env bash
# 本脚本负责检查 x-comments 采集发布、shopping 同步和公开站点的运行新鲜度。
# 它不创建采集任务、不改写数据库；若配置 ALERT_WEBHOOK_URL，失败时只发送脱敏文本告警。

set -Eeuo pipefail

project_dir="${PROJECT_DIR:?必须通过 PROJECT_DIR 指定 x-comments 部署目录}"
shopping_project_dir="${SHOPPING_PROJECT_DIR:?必须通过 SHOPPING_PROJECT_DIR 指定 shopping 部署目录}"
shopping_database="${SHOPPING_MONGO_DATABASE:-choiceshop}"
max_crawl_age_seconds="${MAX_CRAWL_AGE_SECONDS:-1800}"
max_sync_age_seconds="${MAX_SYNC_AGE_SECONDS:-1800}"
alert_webhook_url="${ALERT_WEBHOOK_URL:-}"

notify() {
  local message="$1"
  echo "DEPLOYMENT_ALERT: $message" >&2
  if [[ -z "$alert_webhook_url" ]]; then
    return 0
  fi

  local payload
  payload="$(printf '%s' "$message" | python3 -c 'import json, sys; print(json.dumps({"text": sys.stdin.read()}))')"
  curl --fail --silent --show-error --connect-timeout 5 --max-time 10 \
    -H 'Content-Type: application/json' --data "$payload" "$alert_webhook_url" >/dev/null || \
    echo 'DEPLOYMENT_ALERT: 告警 webhook 投递失败' >&2
}

fail() {
  notify "$1"
  exit 1
}

for seconds in "$max_crawl_age_seconds" "$max_sync_age_seconds"; do
  [[ "$seconds" =~ ^[1-9][0-9]*$ ]] || fail '健康检查阈值必须是正整数秒'
done

health_json="$(curl --fail --silent --show-error --connect-timeout 5 --max-time 10 http://127.0.0.1:8000/health)" || \
  fail 'x-comments /health 不可达'

if ! health_summary="$(python3 - "$health_json" "$max_crawl_age_seconds" <<'PY'
import datetime as dt
import json
import sys

payload = json.loads(sys.argv[1])
max_age = int(sys.argv[2])
if payload.get("status") != "ok" or payload.get("database") != "ok":
    raise SystemExit("x-comments 健康检查未返回数据库正常")
last_success = payload.get("last_successful_crawl_at")
if not last_success:
    raise SystemExit("x-comments 尚无成功采集记录")
parsed = dt.datetime.fromisoformat(last_success.replace("Z", "+00:00"))
age = (dt.datetime.now(dt.timezone.utc) - parsed).total_seconds()
if age > max_age:
    raise SystemExit(f"x-comments 成功采集已滞后 {int(age)} 秒")
revision = payload.get("last_published_revision")
if not isinstance(revision, int) or revision < 0:
    raise SystemExit("x-comments revision 无效")
print(json.dumps({"revision": revision, "crawl_age_seconds": int(age)}))
PY
)"; then
  fail 'x-comments 健康检查数据无效或最后成功采集已超过阈值'
fi

source_revision="$(python3 -c 'import json, sys; print(json.loads(sys.argv[1])["revision"])' "$health_summary")"
sync_json="$(cd "$shopping_project_dir" && docker compose exec -T db mongosh --quiet "$shopping_database" --eval '
const state = db.xianyu_catalog_sync_states.findOne({}, {lastAppliedRevision: 1, lastSuccessfulSyncAt: 1, _id: 0});
print(JSON.stringify(state || {}));
')" || fail '无法读取 shopping 持久化同步游标'

python3 - "$sync_json" "$source_revision" "$max_sync_age_seconds" <<'PY' || fail 'shopping 同步已滞后或游标无效'
import datetime as dt
import json
import sys

state = json.loads(sys.argv[1])
source_revision = int(sys.argv[2])
max_age = int(sys.argv[3])
if source_revision == 0:
    print('{"sync":"not-required"}')
    raise SystemExit(0)
applied = state.get("lastAppliedRevision")
last_sync = state.get("lastSuccessfulSyncAt")
if not isinstance(applied, int) or not last_sync:
    raise SystemExit("shopping 尚未完成首次目录同步")
parsed = dt.datetime.fromisoformat(last_sync.replace("Z", "+00:00"))
age = (dt.datetime.now(dt.timezone.utc) - parsed).total_seconds()
if applied < source_revision and age > max_age:
    raise SystemExit(f"shopping revision 滞后：{applied}/{source_revision}，已 {int(age)} 秒未成功同步")
print(json.dumps({"last_applied_revision": applied, "source_revision": source_revision, "sync_age_seconds": int(age)}))
PY

curl --fail --silent --show-error --connect-timeout 5 --max-time 10 -I http://127.0.0.1/ | \
  grep -q '^X-Powered-By: Next.js' || fail 'shopping 的 Nginx 反向代理不可用'

echo "deployment health check passed: $health_summary"
