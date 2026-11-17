    bash = shutil.which("bash")
    scripts = " ".join(PREPARE_SCRIPTS)
    if not bash:
        raise UpstreamError("bash is required to run the upstream preparation "
                            f"scripts ({scripts}); on Windows use Git Bash")
    env = git_env()

    # Fix the abbreviated payload submodule tag used by this pinned upstream.
    deps_script = stage / "tools/download_deps.sh"
    if not deps_script.is_file():
        raise UpstreamError("tools/download_deps.sh not found in upstream source")

    content = deps_script.read_text()
    old = 'PAYLOAD_TAG=$(git -C "$PAYLOAD_SUBMODULE" describe --tags --always)'
    new = 'PAYLOAD_TAG="v0.1.5-915a65e"'
    if old not in content:
        raise UpstreamError("Could not find expected PAYLOAD_TAG line in tools/download_deps.sh")
    deps_script.write_text(content.replace(old, new, 1))

    for script in PREPARE_SCRIPTS:
        print(f"upstream: preparing {script} ...")
        output = _run([bash, script], cwd=stage, env=env)
        tail = "\n".join(output.strip().splitlines()[-3:])
        if tail:
            print("  " + tail.replace("\n", "\n  "))
    (stage / ".wkal-prepared").write_text("")

def stage(spec=None):
    """Return (path, provenance) for the requested upstream source tree.

    spec: None/'release' -> latest upstream release tag; 'vX.Y.Z' -> that
    tag; an existing directory -> used as-is (already-prepared local clone).
    """
    env = dict(os.environ)
    if spec in (None, "", "release", "latest"):
        tag = env.get("WKAL_UPSTREAM_TAG") or latest_release_tag()
    elif Path(spec).is_dir():
        local = Path(spec).resolve()
        if not _prepared(local):
            _prepare(local)
        commit = None
        if (local / ".git").exists():
            try:
                commit = _run(["git", "rev-parse", "HEAD"], cwd=local).strip()
            except UpstreamError:
                pass
        return local, {"repository": REPO, "source": str(local),
                       "tag": None, "commit": commit, "mode": "local"}
    else:
        tag = spec

    _require_tool("git", "Needed to clone the upstream release.")
    env = git_env()
    commit = _peeled_commit(tag)
    stage = DEPS / tag
    record = stage / ".wkal-upstream.json"
    if record.is_file():
        cached = json.loads(record.read_text())
        if cached.get("commit") == commit:
            if not _prepared(stage):
                _prepare(stage)
            record.write_text(json.dumps(cached, indent=2) + "\n")
            return stage, cached
        print(f"upstream: {tag} moved ({cached.get('commit')[:12]} -> {commit[:12]}); re-cloning")
        _force_rmtree(stage)
    elif stage.exists():
        # Leftover from an interrupted earlier run. Reuse the clone when it
        # still points at the tag and just (re)run the idempotent prepare
        # steps; wipe it when the clone itself is broken.
        try:
            staged_commit = _run(["git", "rev-parse", "HEAD"], cwd=stage, env=env).strip()
        except UpstreamError:
            staged_commit = None
        if staged_commit == commit:
            print(f"upstream: reusing interrupted tree at {tag}; re-running prepare")
            _prepare(stage)
            provenance = {"repository": REPO, "source": REPO_URL, "tag": tag,
                          "commit": commit, "mode": "release-tag"}
            record.write_text(json.dumps(provenance, indent=2) + "\n")
            return stage, provenance
        _force_rmtree(stage)
    print(f"upstream: cloning {REPO_URL} at {tag} ({commit[:12]}) with submodules ...")
    DEPS.mkdir(parents=True, exist_ok=True)
    _run(["git", "clone", "--depth", "1", "--branch", tag,
          "--recurse-submodules", "--shallow-submodules", REPO_URL, stage], env=env)
    staged_commit = _run(["git", "rev-parse", "HEAD"], cwd=stage, env=env).strip()
    if staged_commit != commit:
        _force_rmtree(stage)
        raise UpstreamError(f"cloned {tag} at {staged_commit}, expected {commit}")
    _prepare(stage)
    provenance = {"repository": REPO, "source": REPO_URL, "tag": tag,
                  "commit": commit, "mode": "release-tag"}
    (stage / ".wkal-upstream.json").write_text(json.dumps(provenance, indent=2) + "\n")
    return stage, provenance


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Stage the upstream frontend source tree and print its provenance.")
    parser.add_argument("spec", nargs="?", default=None,
                        help="'release' (default), an upstream tag like v0.4.0, or a local clone path")
    args = parser.parse_args()
    try:
        path, info = stage(args.spec)
    except UpstreamError as error:
        sys.exit(f"error: {error}")
    print(json.dumps({"path": str(path), **info}, indent=2))
