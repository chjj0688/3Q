#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
baseline="$(cat "$root/scripts/kis-baseline.txt")"
[[ "$baseline" =~ ^[a-f0-9]{40}$ ]]
upstream=https://github.com/koreainvestment/open-trading-api.git
export GIT_TERMINAL_PROMPT=0
latest="$(git ls-remote --exit-code "$upstream" HEAD | cut -f1)"
[[ "$latest" =~ ^[a-f0-9]{40}$ ]]
report() {
  printf '%s\n' "$1"
  if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    printf '%s\n' "$1" >> "$GITHUB_STEP_SUMMARY"
  fi
}
report "한투 API 기준 $baseline · 최신 $latest"
if [[ "$baseline" == "$latest" ]]; then
  report "공식 저장소의 기준 커밋과 최신 커밋이 같습니다."
  exit 0
fi
scratch="$(mktemp -d)"
trap 'rm -rf -- "$scratch"' EXIT
git -C "$scratch" init -q
git -C "$scratch" fetch -q --filter=blob:none --depth=1 --no-tags "$upstream" "$baseline" "$latest"
changes="$(git -C "$scratch" diff --no-renames --name-status "$baseline" "$latest" -- \
  kis_devlp.yaml examples_user/kis_auth.py examples_user/overseas_stock \
  examples_llm/kis_auth.py examples_llm/overseas_stock docs stocks_info)"
if [[ -z "$changes" ]]; then
  report "감시 경로에 최종 파일 변경이 없습니다."
  exit 0
fi
report "한투 변경 검토 필요. [공식 변경 비교](${upstream%.git}/compare/$baseline...$latest)"
printf '%s\n' "$changes"
if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
  printf '\n%s\n%s\n%s\n' '```text' "$changes" '```' >> "$GITHUB_STEP_SUMMARY"
fi
exit 1
