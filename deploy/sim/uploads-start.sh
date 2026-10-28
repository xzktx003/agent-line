#!/bin/sh
# Require the existing uploads directory to accept a durable write. Never create it.
set -eu
uploads="${UPLOADS_DIR:-/uploads}"
if [ ! -d "$uploads" ] || [ -L "$uploads" ]; then
    printf '%s\n' 'Sim 上传目录启动受阻：原目录缺失或是符号链接；禁止自动创建空目录。' >&2
    exit 78
fi
chown 1001:1001 "$uploads" || {
    printf '%s\n' 'Sim 上传目录启动受阻：不能确认目录归属；未继续启动。' >&2
    exit 78
}
chmod 700 "$uploads" || {
    printf '%s\n' 'Sim 上传目录启动受阻：不能收紧目录权限；未继续启动。' >&2
    exit 78
}
probe="$uploads/.agent-line-write-probe"
token='agent-line-durable-probe'
unwritable() {
    printf '%s\n' 'Sim 上传目录启动受阻：目录不能持久写入；禁止当作已恢复。' >&2
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
