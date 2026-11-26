#!/bin/bash
set -euo pipefail
umask 077

if [[ "$(uname -s)" != Darwin ]]; then
  echo "이 설치 명령은 macOS에서 실행하세요." >&2
  exit 1
fi

version="${1:-}"
if [[ $# -lt 1 || $# -gt 2 || ! "$version" =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]; then
  echo "사용법: bash scripts/install-macos.sh vX.Y.Z [https://3q.example.com]" >&2
  exit 2
fi

repo="$(cd "$(dirname "$0")/.." && pwd -P)"
support="$HOME/Library/Application Support/3Q"
agent_dir="$HOME/Library/LaunchAgents"
agent="$agent_dir/com.threeq.server.plist"
origin="${2:-}"
if [[ $# == 1 && -f "$support/mobile-origin" ]]; then
  origin="$(cat "$support/mobile-origin")"
fi
python="$(command -v "${THREEQ_PYTHON:-python3}")"
"$python" -c 'import sys; sys.exit(sys.version_info < (3, 11))' ||
  { echo "Python 3.11 이상을 설치하세요." >&2; exit 1; }
mkdir -p "$support/releases" "$support/venvs" "$agent_dir"
scratch="$(mktemp -d "$support/install.XXXXXX")"
trap 'rm -rf -- "$scratch"' EXIT
ref="refs/tags/$version"
git -C "$repo" fetch --no-tags origin "$ref"
commit="$(git -C "$repo" rev-parse --verify 'FETCH_HEAD^{commit}')"
release="$support/releases/$version"
if [[ -d "$release" ]]; then
  if [[ "$(cat "$release/.release-commit")" != "$commit" ]]; then
    echo "이미 설치한 릴리스 태그의 커밋이 바뀌었습니다. 확인이 필요합니다." >&2
    exit 1
  fi
else
  mkdir "$scratch/release"
  git -C "$repo" archive "$commit" | tar -xf - -C "$scratch/release"
  test -f "$scratch/release/scripts/launch-macos.sh"
  printf '%s\n' "$version" > "$scratch/release/.release-version"
  printf '%s\n' "$commit" > "$scratch/release/.release-commit"
  mv "$scratch/release" "$release"
fi

venv="$support/venvs/$version"
if [[ ! -x "$venv/bin/python" ]]; then
  "$python" -m venv "$venv"
fi
"$venv/bin/python" -m pip install --disable-pip-version-check --only-binary=:all: -r "$release/requirements-mobile.txt"
(cd "$release" && "$venv/bin/python" -m unittest discover -s tests)
if [[ -n "$origin" ]]; then
  (cd "$release" && "$venv/bin/python" -c 'import sys; from threeq.mobile import mobile_hostname; mobile_hostname(sys.argv[1])' "$origin")
fi

"$python" - "$support" "$release" "$repo" "$scratch" "$agent" "$origin" <<'PY'
import os
import plistlib
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

support, release, repo, scratch, agent = map(Path, sys.argv[1:6])
origin = sys.argv[6]
current = support / "current"
db = support / "live.sqlite3"
if current.is_symlink() and current.resolve() != release and db.is_file():
    directory = support / "backups"
    directory.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    with closing(sqlite3.connect(db)) as source, closing(sqlite3.connect(directory / f"live-{stamp}.sqlite3")) as target:
        source.backup(target)
for name, value in (("repository-path", repo), ("mobile-origin", origin)):
    staged = scratch / name
    staged.write_text(f"{value}\n")
    os.replace(staged, support / name)
staged_agent = scratch / "launchagent.plist"
with staged_agent.open("wb") as file:
    plistlib.dump({
        "Label": "com.threeq.server",
        "ProgramArguments": ["/bin/bash", str(current / "scripts/launch-macos.sh")],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "StandardOutPath": str(support / "server.log"),
        "StandardErrorPath": str(support / "server.log"),
    }, file)
os.replace(staged_agent, agent)
link = scratch / "current"
link.symlink_to(release)
os.replace(link, current)
PY

domain="gui/$(id -u)"
if /bin/launchctl print "$domain/com.threeq.server" >/dev/null 2>&1; then
  /bin/launchctl bootout "$domain/com.threeq.server"
fi
/bin/launchctl bootstrap "$domain" "$agent"
printf '%s\n' "3Q 서버 $version 설치 완료" "맥 관리자 화면 http://127.0.0.1:8033"
if [[ -n "$origin" ]]; then
  printf '%s\n' "휴대폰 $origin · HTTPS 터널 대상 http://127.0.0.1:8034"
fi
