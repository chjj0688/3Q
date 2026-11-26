#!/bin/bash
set -euo pipefail
umask 077
support="$HOME/Library/Application Support/3Q"
cd "$support/current"
export THREEQ_RELEASE="$(cat .release-version)"
export THREEQ_RELEASE_REPO="$(cat "$support/repository-path")"
python="$(cat "$support/python-path")"
port=8033
url="http://127.0.0.1:$port"

"$python" -m threeq live --port "$port" >> "$support/app.log" 2>&1 &
server=$!
trap 'kill "$server" 2>/dev/null || true' EXIT
trap 'exit 0' TERM INT HUP
for (( attempt=0; attempt<50; attempt++ )); do
  if ! kill -0 "$server" 2>/dev/null; then
    /usr/bin/osascript -e 'display alert "3Q를 시작할 수 없습니다" message "다른 3Q가 실행 중인지 확인하거나 응용 프로그램 지원 폴더의 3Q/app.log를 확인하세요."'
    exit 1
  fi
  if /usr/bin/curl -fsS --max-time 1 "$url/api/state" >/dev/null 2>&1; then
    /usr/bin/open "$url"
    wait "$server"
    exit $?
  fi
  sleep 0.2
done
/usr/bin/osascript -e 'display alert "3Q 시작 시간 초과"'
exit 1
