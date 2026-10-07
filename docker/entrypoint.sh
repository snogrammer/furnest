#!/bin/sh
# Container entrypoint: runs `furnest <args>` as uid 10001.
#
# A bind mount that Docker created on first deploy is owned by root, so uid
# 10001 can't create the SQLite database in it. Started as root, this takes
# ownership of DATA_DIR, then drops privileges before running furnest. Started
# as any other user (e.g. `user: 10001` in compose), it just runs furnest.
#
# Every entry is checked, not just the top level: a `docker exec` as root that
# opened the database leaves root-owned -wal/-shm files, which would make the
# database read-only to the app. DATA_DIR holds only the database and its WAL
# files, so the walk is cheap.
set -eu

if [ "$(id -u)" = 0 ]; then
  dir="${DATA_DIR:-/data}"
  mkdir -p "$dir"
  if [ -n "$(find "$dir" ! -uid 10001 -print -quit)" ]; then
    echo "Taking ownership of $dir for uid 10001" >&2
    chown -R 10001:10001 "$dir"
  fi
  export HOME=/home/furnest USER=furnest LOGNAME=furnest
  exec setpriv --reuid=10001 --regid=10001 --init-groups -- furnest "$@"
fi

exec furnest "$@"
