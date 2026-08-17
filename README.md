# AI Engineering Platform — Deployment Guide

A verified, human-in-the-loop deployment guide and orchestrator for a
local-first AI engineering platform assembled from 24 independent repos
(`github.com/mitkox`) plus Envoy as the unifying gateway. Start with
[DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md) — it's the source of truth for
every command here.

## What's actually in this repo

Only the guide and the tooling built around it:

- [`DEPLOYMENT_GUIDE.md`](DEPLOYMENT_GUIDE.md) — the full runbook, phase by
  phase, every command checked against each component's real source.
- [`inventory.yaml`](inventory.yaml) — the single source of truth for hosts,
  IPs, and which service runs where. Edit this with your own values before
  deploying anything.
- [`deploy-platform.py`](deploy-platform.py) — a confirmation-gated
  orchestrator that automates the phases that are safe to automate (see
  `--list`); everything else in the guide is still a command you run by
  hand, deliberately.
- [`generate-env.py`](generate-env.py) — derives a `.env.platform` file from
  `inventory.yaml` so every tool's connection URL comes from one place.

**The 24 components themselves are not vendored here.** Each is a git
submodule — a pointer to its own independently-owned repository, not a copy
of its code. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for the
full list and each one's license.

## Clone

Submodules aren't checked out by a plain `git clone`:

```bash
git clone --recurse-submodules <this-repo-url>
# or, if you already cloned without that flag:
git submodule update --init --recursive
```

## License

The guide and the two scripts above are MIT-licensed — see
[LICENSE](LICENSE). That license does not extend to any submodule; each one
keeps its own license, set by its own author (local-harness is also an AI
Sweden project, referenced the same way as everything else here). See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for each component's actual
license before redistributing, modifying, or using any of them
independently of this repository.
