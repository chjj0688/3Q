"""Check deployment tags without downloading or running an update."""
import os
import re
import subprocess

TAG = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


def tag_version(text):
    match = TAG.fullmatch(text)
    return tuple(map(int, match.groups())) if match else None


def release_status():
    current = os.environ.get("THREEQ_RELEASE", "")
    repo = os.environ.get("THREEQ_RELEASE_REPO")
    installed = tag_version(current)
    if not installed or not repo:
        return {"current": None, "latest": None, "status": "development"}
    try:
        result = subprocess.run(
            ["git", "-C", repo, "ls-remote", "--refs", "--tags", "origin"],
            check=True, capture_output=True, text=True, timeout=10,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.SubprocessError):
        return {"current": current, "latest": None, "status": "unavailable"}
    versions = []
    for row in result.stdout.splitlines():
        _, separator, ref = row.partition("\t")
        if separator and ref.startswith("refs/tags/"):
            version = tag_version(ref.removeprefix("refs/tags/"))
            if version is not None:
                versions.append(version)
    latest = max(versions, default=None)
    return {
        "current": current,
        "latest": "v" + ".".join(map(str, latest)) if latest else None,
        "status": "update" if latest and latest > installed else "current" if latest else "no-release",
    }
