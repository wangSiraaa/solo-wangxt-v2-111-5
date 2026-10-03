#!/usr/bin/env bash
# 启动用户态 PostgreSQL 15（无 root / docker 环境用）。
# 数据目录：~/.local/pgdata，端口 55432，库名 rawmix。
set -euo pipefail

P="$HOME/.local/pgsql"
PGDATA="$HOME/.local/pgdata"
export LD_LIBRARY_PATH="$P/usr/lib/postgresql/15/lib:${LD_LIBRARY_PATH:-}"
export PATH="$P/usr/lib/postgresql/15/bin:$PATH"

if [ ! -f "$PGDATA/PG_VERSION" ]; then
  # initdb 要求数据目录不存在或为空；sock 目录在启动前再建
  initdb -D "$PGDATA" -U mixapp --auth=trust --no-locale --encoding=UTF8
fi
mkdir -p "$PGDATA/sock"
pg_ctl -D "$PGDATA" -l "$PGDATA/server.log" -w start \
  -o "-p 55432 -c listen_addresses=127.0.0.1 -c unix_socket_directories='$PGDATA/sock'"
sleep 1
psql -h 127.0.0.1 -p 55432 -U mixapp -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='rawmix'" \
  | grep -q 1 || createdb -h 127.0.0.1 -p 55432 -U mixapp rawmix
echo "PostgreSQL ready on 127.0.0.1:55432 (db=rawmix)"
