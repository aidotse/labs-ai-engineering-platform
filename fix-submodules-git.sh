#!/usr/bin/env bash
#
# A normal `git submodule update --init` leaves each submodule's .git as a
# small pointer file (`gitdir: ../.git/modules/<name>`) -- its actual target
# lives in this superproject's .git/modules/, outside whatever directory a
# tool run from inside that submodule treats as its root. That breaks
# anything expecting a self-contained repo there: confirmed with
# vllm-turboquant, whose build derives its version from git history via
# setuptools_scm and fails with "setuptools-scm was unable to detect
# version" as a direct result (see DEPLOYMENT_GUIDE.md Phase 1.1). Nothing
# rules out another of the 26 components hitting the same class of problem
# -- some other build step reading git history, an editor/IDE expecting a
# real .git, etc. -- so this materializes a real .git directory in every
# submodule, not just the one already confirmed to need it.
#
# Run once after cloning or after `git submodule update`. Safe to re-run --
# a no-op for any submodule already materialized.
set -euo pipefail
cd "$(dirname "$0")"

git submodule foreach --quiet --recursive '
  if [ -f .git ]; then
    real_gitdir=$(git rev-parse --git-dir)
    rm .git
    cp -R "$real_gitdir" .git
    # The copied configs worktree line is relative to its OLD location
    # (.git/modules/<name>/, several levels under the superproject root)
    # and is wrong now that this file lives at <name>/.git/config -- drop
    # it; the default (the directory containing .git) is exactly right
    # once .git is co-located with the working tree again.
    grep -v "worktree = " .git/config > .git/config.new
    mv .git/config.new .git/config
    echo "materialized: $(basename "$(pwd)")"
  else
    echo "already a real directory, skipped: $(basename "$(pwd)")"
  fi
'
