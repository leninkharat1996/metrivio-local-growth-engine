#!/usr/bin/env python3
"""
Deterministic decision logic for where the master store comes from at the
start of a workflow run: the actions/cache restore, a fallback artifact
from a previous successful run, or a brand-new empty store.

This module contains no network/GitHub API calls -- it is pure decision
logic operating on data the workflow step already has (whether the cache
restore actually populated data/master, and a list of candidate previous
runs/artifacts already fetched via `gh api`). That keeps it unit-testable
without hitting live GitHub Actions.

The workflow shells out to this module (see google-maps-scraper.yml) to:
  1. decide whether cache, artifact-fallback, or empty applies
     (`select_master_store_source`)
  2. pick which previous run's artifact to restore, deterministically
     (`find_latest_master_store_artifact`)
"""

from __future__ import annotations

MASTER_STORE_ARTIFACT_PREFIX = "google-maps-master-store-"

SOURCE_CACHE = "cache"
SOURCE_ARTIFACT = "artifact"
SOURCE_EMPTY = "empty"


def select_master_store_source(cache_restored: bool, artifact_found: bool) -> str:
    """
    Decide MASTER_STORE_SOURCE given what actually happened so far.

    cache_restored: True if the master store files were actually present
        in data/master/ after the actions/cache/restore step (i.e. either
        an exact key match or a restore-keys prefix match populated them --
        NOT the action's own `cache-hit` output, which is only true on an
        exact key match and would misreport prefix-key restores as misses).
    artifact_found: True if a previous successful run's master-store
        artifact was located (only consulted when cache_restored is False).
    """
    if cache_restored:
        return SOURCE_CACHE
    if artifact_found:
        return SOURCE_ARTIFACT
    return SOURCE_EMPTY


def find_latest_master_store_artifact(runs):
    """
    Given `runs` -- an already GitHub-ordered (newest first) list of
    successful workflow runs, each a dict with at least:
        {"id": <run id>, "artifacts": [{"name": <artifact name>}, ...]}
    -- return (run_id, artifact_name) for the first run (i.e. most recent)
    that has an artifact whose name starts with
    MASTER_STORE_ARTIFACT_PREFIX, or None if no such run/artifact exists.

    This never reorders `runs`: the caller is responsible for querying
    GitHub for successful runs of this workflow only (not arbitrary/failed
    runs, not other workflows), already sorted newest-first, so "first
    match" here is deterministic and always the most recent qualifying
    artifact.
    """
    for run in runs:
        for artifact in run.get("artifacts", []):
            name = artifact.get("name", "")
            if name.startswith(MASTER_STORE_ARTIFACT_PREFIX):
                return run["id"], name
    return None


def _cli(argv):
    """
    Small CLI so the workflow can call this module's logic with plain,
    single-line `python3 .../master_store_source.py <subcommand> ...`
    invocations instead of embedding multi-line Python inside the YAML
    block scalar (which is fragile: unindented heredoc-style Python breaks
    YAML's block-literal indentation rules).
    """
    import json
    import sys

    if not argv:
        print("usage: master_store_source.py <append-run|has-master-artifact|"
              "find-latest|select-source> ...", file=sys.stderr)
        return 2

    command, args = argv[0], argv[1:]

    if command == "append-run":
        entries_path, run_id, artifacts_json = args
        artifacts = json.loads(artifacts_json)
        try:
            with open(entries_path) as f:
                runs = json.load(f)
        except FileNotFoundError:
            runs = []
        runs.append({"id": int(run_id), "artifacts": artifacts})
        with open(entries_path, "w") as f:
            json.dump(runs, f)
        return 0

    if command == "has-master-artifact":
        (artifacts_json,) = args
        artifacts = json.loads(artifacts_json)
        matched = any(
            a.get("name", "").startswith(MASTER_STORE_ARTIFACT_PREFIX)
            for a in artifacts
        )
        return 0 if matched else 1

    if command == "find-latest":
        (entries_path,) = args
        with open(entries_path) as f:
            runs = json.load(f)
        found = find_latest_master_store_artifact(runs)
        if found is None:
            print("")
        else:
            run_id, artifact_name = found
            print(f"{run_id} {artifact_name}")
        return 0

    if command == "select-source":
        cache_restored_str, artifact_found_str = args
        print(
            select_master_store_source(
                cache_restored_str == "true", artifact_found_str == "true"
            )
        )
        return 0

    print(f"unknown subcommand: {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    import sys

    sys.exit(_cli(sys.argv[1:]))
