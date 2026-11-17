"""Resolve and stage the upstream ps5-webkit-autoloader source tree.

The frontend is never committed to this repository. Packaging fetches the
upstream project at its latest GitHub release tag (or a pinned tag, or an
existing local clone) and prepares the serving tree exactly like upstream's
own Makefile does before staging the frontend:

    git clone --depth 1 -b <tag> --recurse-submodules --shallow-submodules
    tools/download_deps.sh         pinned elfldr/kexp/payload release assets
    tools/apply_relapse_patch.sh   third_party/relapse -> frontend copy
    tools/apply_slopkit_patch.sh   third_party/slopkit -> frontend copy
    tools/apply_umtx2_patch.sh     third_party/umtx2 -> frontend copy

The staged tree is cached under .deps/upstream/<tag>/ and reused when its
recorded commit still matches the tag, so repeated builds stay offline and
a re-tagged upstream name is not silently served from stale content.

Requires: git and bash on PATH (the upstream preparation scripts are bash).
Network: api.github.com for release resolution (GITHUB_TOKEN honoured),
github.com for the clone and the pinned payload release assets.
"""
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPS = ROOT / ".deps/upstream"
REFERENCE = json.loads((ROOT / "tools/dependencies.json").read_text())["frontend_reference"]
# dependencies.json stores a full clone URL; accept both URL and owner/name.
REPO = REFERENCE["repository"].removeprefix("https://github.com/").strip("/").removesuffix(".git")
REPO_URL = f"https://github.com/{REPO}.git"
PREPARE_SCRIPTS = ("tools/download_deps.sh",
                   "tools/apply_relapse_patch.sh",
                   "tools/apply_slopkit_patch.sh",
                   "tools/apply_umtx2_patch.sh")
TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


class UpstreamError(RuntimeError):
    pass


def _run(args, cwd=None, env=None):
    result = subprocess.run([str(a) for a in args], cwd=cwd, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if result.returncode != 0:
        raise UpstreamError(f"command failed ({result.returncode}): {' '.join(str(a) for a in args)}\n"
                            + result.stdout.strip())
    return result.stdout


def git_env(base=None):
    """Environment for every git subprocess: worktrees must keep the exact
    bytes upstream committed. checkout CRLF conversion (Git for Windows
    defaults to autocrlf=true) breaks the upstream `git apply` patch steps.
    GIT_CONFIG_* injects into subcommand-spawned git processes too (submodule
    clones, git apply inside the upstream scripts)."""
    env = dict(base if base is not None else os.environ)
    count = int(env.get("GIT_CONFIG_COUNT", "0") or 0)
    existing = {env.get(f"GIT_CONFIG_KEY_{i}") for i in range(count)}
    for key, value in (("core.autocrlf", "false"), ("core.eol", "lf")):
        if key not in existing:
            env[f"GIT_CONFIG_KEY_{count}"] = key
            env[f"GIT_CONFIG_VALUE_{count}"] = value
            count += 1
    env["GIT_CONFIG_COUNT"] = str(count)
    return env


def _github_api(path):
    request = urllib.request.Request("https://api.github.com" + path, headers={
        "User-Agent": "ps5-wkal-esp32s3-build",
        "Accept": "application/vnd.github+json",
    })
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        request.add_header("Authorization", "Bearer " + token)
    last = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read())
        except Exception as exc:  # rate limits and transient CDN drops
            last = exc
            time.sleep(2 * attempt + 1)
    raise UpstreamError(f"GitHub API request failed: {path} ({last})")


def _peeled_commit(tag):
