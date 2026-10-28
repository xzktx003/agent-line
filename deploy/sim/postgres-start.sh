#!/bin/sh
# Normal starts only open an existing cluster. Initial creation is explicit.
set -eu
pgdata="${PGDATA:-/var/lib/postgresql/data}"
if [ ! -f "$pgdata/PG_VERSION" ] || [ ! -s "$pgdata/PG_VERSION" ] || [ -L "$pgdata/PG_VERSION" ] ||
   [ ! -d "$pgdata/global" ] || [ -L "$pgdata/global" ] ||
   [ ! -f "$pgdata/global/pg_control" ] || [ ! -s "$pgdata/global/pg_control" ] || [ -L "$pgdata/global/pg_control" ] ||
   [ ! -d "$pgdata/base" ] || [ -L "$pgdata/base" ] ||
   [ ! -d "$pgdata/pg_wal" ] || [ -L "$pgdata/pg_wal" ]; then
    printf '%s\n' 'Sim 数据库启动受阻：原集群数据缺失或不完整；请恢复原数据，禁止自动初始化。' >&2
    exit 78
fi
# A missing cluster is already rejected. Also refuse a cluster directory that
# cannot durably store a write, and never leave the probe or a symlink behind.
probe="$pgdata/.agent-line-write-probe"
token='agent-line-durable-probe'
unwritable() {
    printf '%s\n' 'Sim 数据库启动受阻：数据目录不能持久写入；禁止自动初始化。' >&2
    exit 78
}
if [ -L "$probe" ]; then
    unwritable
fi
if [ -e "$probe" ]; then
    if [ -f "$probe" ] && [ "$(cat "$probe")" = "$token" ]; then
        rm -f -- "$probe" || unwritable
    else
        unwritable
    fi
fi
if ! printf '%s\n' "$token" | dd of="$probe" conv=fsync status=none; then
    if [ -f "$probe" ] && [ ! -L "$probe" ]; then
        rm -f -- "$probe" || true
    fi
    unwritable
fi
if [ -L "$probe" ] || [ "$(cat "$probe")" != "$token" ]; then
    if [ -f "$probe" ] && [ ! -L "$probe" ]; then
        rm -f -- "$probe" || true
    fi
    unwritable
fi
rm -f -- "$probe" || unwritable
if [ -e "$probe" ] || [ -L "$probe" ]; then
    unwritable
fi
exec /usr/local/bin/docker-entrypoint.sh "$@"
