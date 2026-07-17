#!/usr/bin/env bash
# 本脚本负责对 x-comments 的 PostgreSQL 容器执行每日逻辑备份并保留有限历史。
# 它只调用容器内的 pg_dump，不读取或打印数据库密码、登录态或服务间令牌；恢复和告警由独立运维流程负责。

set -Eeuo pipefail

project_dir="${PROJECT_DIR:?必须通过 PROJECT_DIR 指定 x-comments 部署目录}"
backup_dir="${BACKUP_DIR:-/var/backups/x-comments/postgresql}"
retention_days="${RETENTION_DAYS:-7}"

if ! [[ "$retention_days" =~ ^[1-9][0-9]*$ ]]; then
  echo "RETENTION_DAYS 必须是正整数" >&2
  exit 2
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "未找到 docker，无法执行 PostgreSQL 备份" >&2
  exit 2
fi

umask 077
install -d -m 700 "$backup_dir"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
final_path="$backup_dir/x-comments-postgres-$timestamp.dump"
temporary_path="$(mktemp "$backup_dir/.x-comments-postgres-$timestamp.XXXXXX.dump")"

cleanup() {
  rm -f -- "$temporary_path"
}
trap cleanup EXIT

cd "$project_dir"
docker compose exec -T postgres sh -c \
  'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom --no-owner --no-privileges' \
  >"$temporary_path"

if [[ ! -s "$temporary_path" ]]; then
  echo "pg_dump 未产生可用备份文件" >&2
  exit 1
fi

mv -- "$temporary_path" "$final_path"
sha256sum "$final_path" >"$final_path.sha256"
chmod 600 "$final_path" "$final_path.sha256"

# 保留最近 N 天的备份及校验文件；仅删除本脚本生成的固定前缀文件。
find "$backup_dir" -maxdepth 1 -type f \
  \( -name 'x-comments-postgres-*.dump' -o -name 'x-comments-postgres-*.dump.sha256' \) \
  -mtime +$((retention_days - 1)) -delete

echo "x-comments PostgreSQL backup completed: $(basename "$final_path")"
