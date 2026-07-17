#!/usr/bin/env bash
# 本脚本负责将最新 PostgreSQL 逻辑备份恢复到无网络的临时容器并做只读校验。
# 它属于 x-comments 运维模块，与 backup_postgres.sh 配合验证备份可用于回滚。
# 本脚本不连接、修改或重启生产 PostgreSQL，也不处理 shopping 的 MongoDB 数据。

set -euo pipefail

backup_dir="${BACKUP_DIR:-/var/backups/x-comments/postgresql}"
restore_image="${RESTORE_IMAGE:-postgres:16-alpine}"
container_name="${RESTORE_CONTAINER_NAME:-x-comments-backup-restore-check}"

# 查找最新的 custom-format PostgreSQL 备份。
# 参数：无。
# 返回：向标准输出写入备份的绝对路径；没有可用备份时以非零状态退出。
find_latest_dump() {
  find "$backup_dir" -maxdepth 1 -type f -name '*.dump' -printf '%T@ %p\n' \
    | sort -nr \
    | head -n 1 \
    | cut -d' ' -f2-
}

# 停止临时恢复容器，确保失败分支也不会残留容器。
# 参数：无。
# 副作用：若容器存在则停止它；停止失败不会遮蔽原始错误。
cleanup() {
  docker stop "$container_name" >/dev/null 2>&1 || true
}

# 等待临时 PostgreSQL 接受连接。
# 参数：无。
# 返回：30 秒内就绪时返回 0；超时时返回非零。
wait_for_database() {
  local attempt
  for attempt in $(seq 1 30); do
    if docker exec "$container_name" pg_isready -U postgres -d postgres >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

# 主流程：校验最新备份、恢复到临时无网络容器，并检查核心表是否存在。
# 参数：无。
# 返回：成功时输出表数和 catalog revision 数；任何校验或恢复失败时以非零状态退出。
main() {
  local dump_path table_count revision_count

  dump_path="$(find_latest_dump)"
  if [[ -z "$dump_path" || ! -f "$dump_path" || ! -f "${dump_path}.sha256" ]]; then
    echo "未找到带 SHA-256 校验文件的 PostgreSQL 备份：$backup_dir" >&2
    return 1
  fi

  sha256sum --check "${dump_path}.sha256"
  if docker ps -a --format '{{.Names}}' | grep -Fxq "$container_name"; then
    echo "临时恢复容器名称已被占用：$container_name" >&2
    return 1
  fi

  trap cleanup EXIT
  docker run -d --rm --name "$container_name" --network none \
    -e POSTGRES_HOST_AUTH_METHOD=trust "$restore_image" >/dev/null
  if ! wait_for_database; then
    docker logs "$container_name" >&2 || true
    return 1
  fi

  cat "$dump_path" | docker exec -i "$container_name" \
    pg_restore --no-owner --no-privileges --clean --if-exists -U postgres -d postgres

  table_count="$(docker exec "$container_name" psql -U postgres -d postgres -Atc \
    "select count(*) from information_schema.tables where table_schema = 'public';")"
  revision_count="$(docker exec "$container_name" psql -U postgres -d postgres -Atc \
    'select count(*) from catalog_revisions')"
  if [[ "$table_count" -le 0 ]]; then
    echo '备份恢复后没有 public 表。' >&2
    return 1
  fi

  printf 'isolated_restore=passed tables=%s catalog_revisions=%s dump=%s\n' \
    "$table_count" "$revision_count" "$(basename "$dump_path")"
}

main "$@"
