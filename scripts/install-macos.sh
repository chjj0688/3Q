#!/bin/bash
set -euo pipefail
umask 077

if [[ "$(uname -s)" != Darwin ]]; then
  echo "이 설치 명령은 macOS에서 실행하세요." >&2
  exit 1
fi

version="${1:-}"
if [[ $# != 1 || ! "$version" =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]; then
  echo "사용법: bash scripts/install-macos.sh vX.Y.Z" >&2
  exit 2
fi

repo="$(cd "$(dirname "$0")/.." && pwd -P)"
support="$HOME/Library/Application Support/3Q"
app="$HOME/Applications/3Q.app"
python="$(command -v "${THREEQ_PYTHON:-python3}")"
"$python" -c 'import sys; sys.exit(sys.version_info < (3, 11))' ||
  { echo "Python 3.11 이상을 설치하세요." >&2; exit 1; }
mkdir -p "$support/releases" "$HOME/Applications"
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
  (cd "$scratch/release" && "$python" -m unittest discover -s tests)
  printf '%s\n' "$version" > "$scratch/release/.release-version"
  printf '%s\n' "$commit" > "$scratch/release/.release-commit"
  mv "$scratch/release" "$release"
fi

"$python" - "$support" "$release" "$repo" "$python" "$scratch" "$app" <<'PY'
import os
import plistlib
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from shutil import copyfile

support, release, repo, python, scratch, app = map(Path, sys.argv[1:])
current = support / "current"
db = support / "live.sqlite3"
if current.is_symlink() and current.resolve() != release and db.is_file():
    directory = support / "backups"
    directory.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    with closing(sqlite3.connect(db)) as source, closing(sqlite3.connect(directory / f"live-{stamp}.sqlite3")) as target:
        source.backup(target)
contents = app / "Contents"
executable = contents / "MacOS" / "3Q"
executable.parent.mkdir(parents=True, exist_ok=True)
launcher = scratch / "launcher"
copyfile(release / "scripts/launch-macos.sh", launcher)
launcher.chmod(0o755)
os.replace(launcher, executable)
with (contents / "Info.plist").open("wb") as file:
    plistlib.dump({
        "CFBundleIdentifier": "com.threeq.dashboard",
        "CFBundleName": "3Q",
        "CFBundleExecutable": "3Q",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": release.name[1:],
    }, file)
for name, value in (("repository-path", repo), ("python-path", python)):
    staged = scratch / name
    staged.write_text(f"{value}\n")
    os.replace(staged, support / name)
link = scratch / "current"
link.symlink_to(release)
os.replace(link, current)
PY

printf '%s\n' "3Q $version 설치 완료 · $app" "다음 실행부터 선택한 버전을 사용합니다."
