#!/bin/sh
# Ordinary starts only open an existing Redis dataset. They never create an empty one.
set -eu
data="${REDIS_DATA:-/data}"
if [ ! -d "$data" ] || [ -L "$data" ] ||
   [ ! -d "$data/appendonlydir" ] || [ -L "$data/appendonlydir" ] ||
   [ ! -f "$data/dump.rdb" ] || [ ! -s "$data/dump.rdb" ] || [ -L "$data/dump.rdb" ]; then
    printf '%s\n' 'Sim Redis 启动受阻：原持久数据缺失或不完整；禁止自动创建空实例。' >&2
    exit 78
fi
probe="$data/.agent-line-write-probe"
token='agent-line-durable-probe'
unwritable() {
    printf '%s\n' 'Sim Redis 启动受阻：数据目录不能持久写入；禁止自动创建空实例。' >&2
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
