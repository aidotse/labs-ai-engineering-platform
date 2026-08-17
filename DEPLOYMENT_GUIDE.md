# AI Engineering Platform — Deployment Guide

A local-first AI engineering platform assembled from 24 independent repos (`github.com/mitkox`) plus one added component: Envoy, used as the unifying gateway (replacing an earlier LiteLLM-based design). Every command in this guide has been checked against the real source of the repo it deploys — not assumed from a README. Where a repo behaves differently from what its own documentation implies, that's called out inline, once, at the point where it matters.

**This guide's commands are the source of truth; `deploy-platform.py` is a convenience layer on top of them, not a replacement.** No script here provisions a host, installs a base OS, or sets up SSH access — every host must already exist and already accept your key before you start. Within that boundary, `deploy-platform.py` *does* read `inventory.yaml` and chain several phases together (1, 2, 4, 6.3, 7.8 — see [Automated Deployment](#automated-deployment-human-in-the-loop)), each step gated behind a confirmation prompt before it builds an image, restarts a shared service, or touches a placeholder host. Everything else in this guide (Phase 3, 5, most of 6 and 7) is still copy-paste-by-hand, one command at a time. See [Prerequisites by Host Type](#prerequisites-by-host-type) for exactly what "already set up" needs to mean before Phase 1.

> **Before you start:** edit [`inventory.yaml`](./inventory.yaml) with your real IPs, SSH users, and hostnames — it's the source of truth for every address in this guide. Two kinds of placeholder appear below: obvious ones like `<your-model-name>` and `sk-REPLACE-ME`, and **every single numeric IP address** (`10.0.1.10`, `10.0.4.10`, and so on). The IPs are not disguised as placeholders — they're written as concrete addresses because that's what a working command needs to look like — but **none of them are real**. They're `inventory.yaml`'s own example values, chosen to match its `# Replace placeholder IPs (10.0.x.x)` convention, and this network doesn't exist anywhere outside this document. Every command that references a host resolves through the table below — look up the real IP for that host in `inventory.yaml` and substitute it before running anything.

| Role | Example hostname | Example IP used in this guide | `inventory.yaml` location |
|---|---|---|---|
| Mac workstation (primary) | `mac-dev-01` | `10.0.1.10` | `mac_workstations[0]` |
| Mac workstation (secondary) | `mac-dev-02` | `10.0.1.11` | `mac_workstations[1]` |
| Jetson, primary edge | `jetson-01` | `10.0.2.10` | `jetson_nodes.nodes[0]` |
| Jetson, edge inference | `jetson-02`…`jetson-05` | `10.0.2.11`–`10.0.2.14` | `jetson_nodes.nodes[1..4]` |
| Linux GPU, L40S | `linux-gpu-01` | `10.0.3.10` | `linux_gpu_hosts[0]` |
| Linux GPU, L4 | `linux-gpu-02` | `10.0.3.11` | `linux_gpu_hosts[1]` |
| Linux GPU, Gaudi2 | `linux-gpu-03` | `10.0.3.12` | `linux_gpu_hosts[2]` |
| Linux GPU, AMD | `linux-gpu-04` | `10.0.3.13` | `linux_gpu_hosts[3]` |
| Linux CPU, gateway/sandbox | `linux-cpu-01` | `10.0.4.10` | `linux_cpu_hosts[0]` |
| Linux CPU, overflow | `linux-cpu-02` | `10.0.4.11` | `linux_cpu_hosts[1]` |
| OpenShift GPU node (example only — `inventory.yaml` doesn't fix a subnet for these; pick real ones from `oc get nodes -o wide`) | `ocp-worker-gpu-01`/`02` | `10.0.5.10`/`.11` | `openshift_cluster.gpu_nodes[0..1]` |

**A Mac's IP is a different kind of placeholder than everyone else's.** Every other host above is server-class and its IP is assumed DHCP-reserved or otherwise stable. A laptop's isn't — different wifi network, sleep/wake, lease renewal, and it's a different address than yesterday. That's a non-issue as long as the Mac only ever connects *out* (to Envoy, rlmgw, the GPU fleet — its normal role, and the only one it plays if it's not running `ds4-zgx-gb10`). It only matters if something else needs to reach the Mac — `ds4-zgx-gb10` as an Envoy backend, or `ain-node` peering with it. For those cases, use `<hostname>.local` (mDNS/Bonjour, already built into macOS, nothing to install or run) instead of a raw IP — `deploy-platform.py`'s Envoy config generator already does this automatically for any Mac running `ds4-zgx-gb10`, and `ain-node` has its own built-in mDNS peer discovery (`--mdns`, on by default) for exactly this reason. mDNS only resolves for peers on the same LAN segment as the Mac — same constraint it always has.

---

## Contents

1. [Architecture](#architecture) — what this platform is and how its pieces fit together
2. [GPU Vendor Support Matrix](#gpu-vendor-support-matrix) — which repo runs on which hardware
3. [Choosing Models for Your Hardware](#choosing-models-for-your-hardware) — which components let you pick a model, and how to size one
4. [Deployment Path](#deployment-path) — what order to build this in, and what's optional
5. [Prerequisites by Host Type](#prerequisites-by-host-type)
6. [Deployment Helper Script](#deployment-helper-script)
7. [Automated Deployment (Human-in-the-Loop)](#automated-deployment-human-in-the-loop) — `generate-env.py` and `deploy-platform.py`, and exactly what each does and doesn't automate
8. [Phase 1 — Model Serving](#phase-1--model-serving)
9. [Phase 2 — Gateway](#phase-2--gateway)
10. [Phase 3 — Agent Execution](#phase-3--agent-execution)
11. [Phase 4 — Security Pipeline](#phase-4--security-pipeline)
12. [Phase 5 — Model Optimization](#phase-5--model-optimization)
13. [Phase 6 — Edge Agents](#phase-6--edge-agents)
14. [Phase 7 — Developer Tools](#phase-7--developer-tools)
15. [Network Topology & Firewall Rules](#network-topology--firewall-rules)
16. [Where Configuration Actually Lives](#where-configuration-actually-lives) — every component's real config surface
17. [API Examples](#api-examples)
18. [Monitoring & Troubleshooting](#monitoring--troubleshooting)
19. [Component Reference](#component-reference) — every component, standalone, one entry each

---

## Architecture

**What this is:** a set of independently-deployable tools — model servers, gateways, sandboxed agent runners, a security-review pipeline, model-optimization jobs, an edge agent mesh, and developer tooling — that share nothing at the code level. Nothing here is a distributed system with internal RPCs between components; every integration point is a config value (a URL, a port) that you set, not a hard-coded assumption in the software. That property is documented per-component in the [Component Reference](#component-reference) at the end of this guide, and it's why the phases below can be built in almost any order, or skipped outright.

**The two things that actually unify the platform** — one added, one a mitkox repo used for this specific job:

- **Envoy** (Phase 2.1) — a single OpenAI-compatible endpoint that routes by model name across every serving backend (Mac, OpenShift, Jetson) and fails over automatically if one goes down. This is convenience, not a requirement: anything that talks to Envoy can instead talk directly to whichever backend it wants. It runs on `linux-cpu-01` — a shared host, not a dedicated one; it sits alongside rlmgw, `ain-node`, `firecracker-agentfs`, and aegis, which are already there. It needs a plain Linux box specifically because it has to reach three backends on three different network segments: OpenShift (only reachable from outside the cluster via its Route), a Mac workstation, and a Jetson, the latter two on the plain LAN. A pod running inside OpenShift's own network can't reach the Mac/Jetson subnets without real SDN work (static routes, egress rules); a Linux host already sitting on that LAN reaches all three for free.
- **rlmgw** (Phase 2.2) — sits in front of exactly one backend and injects context from one codebase into requests. Narrower than Envoy on purpose: it's for coding-assistant workloads (megacode, SkillOpt, agents) where automatic repo context is worth more than backend breadth.

Everything else — sandboxing, security scanning, model optimization, the edge mesh, developer tools — reads one of these two (or nothing at all, or a completely external endpoint) via a single configurable `--base-url`/`endpoint`/`api_base` field. There is no phase that hard-requires a prior phase to have run, with one real exception: you need *some* model serving somewhere before any LLM-consuming tool is useful, though it doesn't have to be this platform's own (SDFT's teacher, for example, can point at any vLLM instance anywhere).

```
┌───────────────────────────────────────────────────────────────────────────┐
│                    AI Engineering Platform — topology                     │
│                                                                             │
│  🍎 Mac Devs (10.0.1.x)              🤖 Jetsons (10.0.2.x)                │
│  ├── ds4-server :8080 (DwarfStar)    ├── Ollama :11434                    │
│  │   local DeepSeek-V4/GLM-5.2 GGUF  │   (verify GPU accel — Phase 1.3)   │
│  └── Ollama :11434 (dev sandbox)     └── ain-node, oda-r (oda needs an    │
│                                            interactive session — 6.1)     │
│                                                                             │
│  🔀 linux-cpu-01 (10.0.4.10)         ☸️  OpenShift Cluster (L4/L40S,      │
│  ├── Envoy :4000 — unifying gw             maybe Gaudi2 — confirm which)  │
│  │   model routing + auto-failover   ├── vllm-turboquant :8000            │
│  ├── rlmgw :8010 — one backend,      ├── sonic :9000 (WS gateway)         │
│  │   one repo, adds context          ├── fabrica :8080 (Kata sandbox)     │
│  ├── ain-node :8787/:4001            └── background-coding-agents,        │
│  └── firecracker-agentfs                 ai-coding-factory (scaffold      │
│      (independent of fabrica)             tool, not a service)           │
│                                                                             │
│  🐧 Linux GPU (10.0.3.x): L40S/L4 (SDFT, sparser-faster-llms — adapted    │
│      for CUDA 12.x, Phase 5) + Gaudi2 (idle, no repo supports Habana) +   │
│      AMD (vllm-turboquant via ROCm build)                                 │
└───────────────────────────────────────────────────────────────────────────┘
```

---

## GPU Vendor Support Matrix

The real fleet is **NVIDIA L4, NVIDIA L40S, Intel Gaudi2, and AMD** (model TBD) — check `inventory.yaml` for which host has which. Use this table before running any Phase 1 or Phase 5 step on a given host.

| Repo | NVIDIA (CUDA) | AMD (ROCm) | Intel (Habana/Gaudi) | Apple (Metal/CoreML) | Notes |
|---|---|---|---|---|---|
| `vllm-turboquant` | ✅ Yes | ✅ Yes | ❌ No | ❌ No | Real Dockerfiles for both (`docker/Dockerfile`, `docker/Dockerfile.rocm`). The fork's namesake feature — TurboQuant KV-cache quantization — only activates on NVIDIA **RTX A6000/SM86** or **GB10/SM121**; L4/L40S are Ada (**SM89**), so it's inactive on the real fleet too, same as on AMD. Treat it as plain vLLM 0.19 unless an A6000 is added. No Habana build exists — **Gaudi2 has no serving path here.** |
| `ds4-zgx-gb10` (DwarfStar) | ⚠️ Builds, likely can't serve | ⚠️ Builds, likely can't serve | ❌ No | ⚠️ Primary target, but see caveat | **Builds on both NVIDIA (`make cuda-spark` for GB10, `make cuda-generic` for any local CUDA GPU — genuinely broader than AMD, which has no generic ROCm path at all, only `gfx1151`/Strix Halo) and Metal — but confirmed from `download_model.sh --help`, every model this project ships is far larger than typical desktop/laptop/edge memory: DeepSeek V4 Flash's smallest quant (`q2-imatrix`) is already 81GB on disk, "recommended for 96 and 128GB RAM machines"; PRO variants are 412–430GB; GLM-5.2's officially-validated quants (the only ones scored against the project's own test fixture) are 262–434GB.** There is no documented model variant that fits under roughly 80GB of usable memory — not on a 16–48GB GPU, not on a 32GB Jetson, not on a 16–32GB Mac laptop. This is only realistic on a genuinely high-memory machine (96GB+ unified-memory Mac Studio, or true GB10/DGX-Spark-class hardware) — treat as **not deployable** on typical laptop/workstation/edge-class hardware regardless of vendor. |
| `SDFT` | ✅ Adaptable | ❌ No | ❌ No | ❌ No | Ships only nightly-CUDA-13.1/GB10 instructions; adapted here for stable CUDA 12.x on L4/L40S (Phase 5.1) — treat as untested, not confirmed-working. |
| `sparser-faster-llms` | ⚠️ Uncertain on L4/L40S | ❌ No | ❌ No | ❌ No | Custom CUDA kernels documented as "designed for H100 GPUs" (Hopper, SM90). L4/L40S are Ada (SM89) — may be a real architecture mismatch, not just a version gap. |
| `oda` | ⚠️ Effectively CPU-only | n/a | n/a | n/a | GPU auto-detection exists but is dead code (never called) — `--no-gpu` is currently a no-op either way. |
| `Thinking-with-Visual-Primitives` | n/a — no code | n/a | n/a | ❌ No | Ships a paper (PDF) only; zero `.py` files in the repo. Nothing to deploy. |
| `rlmgw`, `sonic`, `aegis`, `megacode`, `SkillOpt`, `oda-r`, `ai-coding-factory`, `background-coding-agents` | — | — | — | — | Vendor-agnostic: they call an OpenAI-compatible HTTP endpoint and don't care what's serving it. |
| `ain`, `dede`, `ccar`, `fteplusai`, `aimatch`, `fabrica`, `firecracker-agentfs` | — | — | — | — | No GPU dependency at all. |

**No repo in this workspace has a Habana/SynapseAI code path.** If a host turns out to be Gaudi2, it's idle from this platform's point of view unless you build a separate Habana-native stack.

---

## Choosing Models for Your Hardware

Only two components in this platform let you pick your own model — everything else is either hardcoded to a fixed set or has no model-choice concept at all:

| Component | Model choice? |
|---|---|
| **vllm-turboquant** (Phase 1.1) | Yes — any Hugging Face repo, passed straight to `vllm serve` |
| **Ollama** (Phase 1.3, also usable on Mac) | Yes — anything in Ollama's library, or an imported GGUF |
| **SDFT** (Phase 5.1) | Partial — the student is a free choice via `--model_name_or_path`; the teacher is whatever you point `--vllm_server_base_url` at |
| `ds4-zgx-gb10` (Phase 1.2) | **No** — hardcoded to the specific DeepSeek V4/GLM-5.2 GGUFs `download_model.sh` fetches. Moot anyway on hardware under ~96GB — see the matrix above |
| `sparser-faster-llms` (Phase 5.2) | **No** — trains one of four fixed small architectures (0.5B–2B params) from `cfgs/run_cfg/`, not an arbitrary imported model |

### Sizing rule of thumb

```
model size (GB) ≈ params (billions) × bytes-per-parameter
                 + KV-cache/activation overhead (roughly 10-20% more, growing with context length)
```

| Precision | Bytes/param | Where you'd use it |
|---|---|---|
| FP16/BF16 | 2.0 | `vllm serve`'s default when the model has no quant in its name |
| FP8 | 1.0 | `vllm serve --quantization fp8` (Ada/Hopper-class GPUs and newer) |
| INT8 / Q8_0 | 1.0 | Ollama's `q8_0` tag; AWQ/GPTQ 8-bit |
| Q4_K_M / AWQ / GPTQ 4-bit | ~0.55–0.6 | The default "good quality, small footprint" choice for both vLLM and Ollama |

### What that means for your specific fleet

Leave headroom — plan against roughly 80% of a card's VRAM, not 100%, to leave room for KV cache and driver overhead. Longer context windows eat directly into this same budget.

| Your hardware | Usable budget (~80%) | Comfortable at Q4/AWQ | Comfortable at FP16/FP8 |
|---|---|---|---|
| 16GB NVIDIA | ~13GB | 7–8B (Llama 3.1 8B, Qwen2.5-7B) | ~6B |
| 32GB (Jetson, AMD low end) | ~26GB | 13–14B (Qwen2.5-14B); ~30B is tight | 8–13B |
| 48GB (NVIDIA/AMD high end) | ~38GB | 30–34B (Qwen2.5-32B); up to ~70B at aggressive Q3/Q4 | 13–20B |
| Mac 16–32GB (unified) | same GB math as the equivalent NVIDIA row | as above | as above, but the OS and other running apps share the same pool — leave more headroom than a dedicated GPU |

Starting points, not guarantees — always confirm against the specific model card's actual published size before committing a deployment.

### Picking a model per component

**vllm-turboquant** — pass any repo to `vllm serve`, and set `--max-model-len` deliberately (the default is often the model's full trained context, which may not fit your budget even if the weights do):

```bash
vllm serve Qwen/Qwen2.5-14B-Instruct-AWQ --tensor-parallel-size 1 --max-model-len 8192
```

Prefer models already published pre-quantized (an `AWQ`/`GPTQ`/`FP8` tag in the repo name) — `vllm serve` doesn't quantize on the fly by default.

**Ollama** — pull by tag; check the model's library page for the exact quantized size before pulling on a memory-constrained Jetson:

```bash
ollama pull qwen2.5:14b     # ~9GB at Ollama's default Q4_K_M
ollama pull llama3.1:8b     # ~4.9GB
```

**SDFT** — the student model in Phase 5.1's `--model_name_or_path` is a free choice; the README's own example (`Qwen/Qwen3-0.6B`) is tiny and fits anywhere in this fleet without a second thought. The teacher runs on a separate vLLM instance and never has to fit on the training GPU at all.

---

## Deployment Path

Nothing here is a strict pipeline — see [Architecture](#architecture). This section is the practical version: what to build first, what each later phase actually needs, and what happens if you stop early.

**Minimum useful deployment: Phase 1, one backend, nothing else.** Build `vllm-turboquant` on whatever GPU you have, or use Ollama (Phase 1.3) on modest hardware, and you have a working OpenAI-compatible endpoint. `ds4-zgx-gb10` is Phase 1's third option but needs ~96GB+ RAM for even its smallest model — see the [GPU Vendor Support Matrix](#gpu-vendor-support-matrix) before counting on it. Every other phase is additive from there.

| Phase | Needs before it | Skippable? | What you lose if you skip it |
|---|---|---|---|
| **1 — Model Serving** | Just the target hardware | No — this is the floor everything else stands on | Nothing to point any other tool at |
| **2 — Gateway** (Envoy, rlmgw) | Phase 1 (needs at least one backend to route to) | **Yes, entirely.** Point tools directly at a Phase 1 backend's `/v1` endpoint instead | No unified endpoint, no automatic failover, no repo-context injection for coding tools |
| **3 — Agent Execution** (fabrica, firecracker-agentfs, background-coding-agents, ai-coding-factory) | Kubernetes+Kata (fabrica) or KVM (firecracker-agentfs); an endpoint from Phase 1/2 for the other two | Yes — each of the four independently | Sandboxed autonomous coding agents; the other three repos are unrelated to each other too, see [Component Reference](#component-reference) |
| **4 — Security Pipeline** (aegis, megacode, tnt) | An endpoint from Phase 1/2 (all optional/configurable) | Yes — each independently | Signed package installs, AI-assisted security review |
| **5 — Model Optimization** (SDFT, sparser-faster-llms) | A CUDA GPU + (for SDFT) any external vLLM teacher endpoint | Yes | Distilled/sparsified models for the edge tier — Phase 6 works fine with stock Ollama models instead |
| **6 — Edge Agents** (oda, oda-r, ain) | A Jetson (oda, oda-r); nothing (ain runs anywhere) | Yes — each independently | Edge dev-environment automation, edge reasoning loop, P2P agent mesh |
| **7 — Developer Tools** | Varies per tool — see each subsection | Yes — each independently | Convenience tooling only; none of it serves other phases |

**If you're starting today:** Phase 1 (pick one backend) → optionally Phase 2 if you want more than one backend unified → everything else in whatever order matches what you actually need, skipping freely. The [Component Reference](#component-reference) at the end gives the minimal standalone command for every single component if you'd rather start somewhere in the middle.

---

## Prerequisites by Host Type

**Read this before running anything below.** This guide is a manual runbook first — nothing here provisions a machine or installs a base OS on its own. `deploy-platform.py` does read `inventory.yaml` and run some of these exact commands for you over SSH, confirmation-gated (Phase 1, 2, 4, 6.3, 7.8 — see [Automated Deployment](#automated-deployment-human-in-the-loop)); everything else in every other phase is something *you* (or an agent working on your behalf) still run by hand, one at a time, over SSH or against `docker`/`oc`/`helm`. Concretely:

- **Every host must already exist before you start.** `linux-cpu-01`, `linux-gpu-01`, `jetson-01`, `mac-dev-01`, the OpenShift nodes — this guide does not create VMs, provision cloud instances, or image bare metal. It assumes each one is already a running machine with a base OS installed (Ubuntu/Debian-like for the Linux hosts specifically — the `apt install` commands below assume that; substitute your distro's package manager if different) and reachable on the network at whatever IP you put in `inventory.yaml`.
- **SSH key access must already work to every Linux/Jetson host before you run a single command here.** Every deploy step in this guide is literally `ssh <user>@<ip> "..."` or `./deploy.sh <repo> <user>@<ip> "..."`, run from your Mac (or wherever you're operating from). That means the SSH key at `inventory.yaml`'s `global.ssh_key` (`~/.ssh/id_ed25519` by default) needs to already be authorized on each target host, for the `ssh_user` that host's `inventory.yaml` entry specifies (`admin` for Linux hosts, `nvidia` for Jetsons). If that's not set up yet, do it first — from your Mac, for each host:
  ```bash
  ssh-copy-id -i ~/.ssh/id_ed25519.pub admin@10.0.4.10
  ssh admin@10.0.4.10 "echo ok"   # confirms it worked, no password prompt
  ```
- **`inventory.yaml` is the source of truth, read programmatically by exactly two scripts.** `deploy-platform.py` parses it to run Phase 1/2/4/6.3/7.8's automated steps, and `generate-env.py` parses it to emit `.env.platform`; `deploy.sh` does not, and nothing else does either — it's a plain YAML file you edit by hand, not a live config store with a daemon watching it. For the phases those two scripts cover, changing an IP and re-running picks it up automatically (Envoy's backend list, for instance, regenerates from whatever's currently in `inventory.yaml` — see [2.1](#phase-2--gateway)); for every other phase in this guide, you still have to find and replace that same value in each command by hand.
- **`deploy.sh`** (below) is a separate, smaller thing: a convenience wrapper, not an orchestrator — it `rsync`s one repo to one host and runs one setup command there. It doesn't read `inventory.yaml`, doesn't know phase order, doesn't chain steps together — you still invoke it once per repo, per host, by hand. (`deploy-platform.py`, covered next, is the one that does chain steps together — for the phases it covers.)
- **Most things still don't install themselves.** Phase 3, 5, and most of 6 and 7 have no entry in `deploy-platform.py` at all — each one's "Deploy" section in this guide is the actual, complete list of commands you (or an agent) must run against that host by hand. `deploy-platform.py` walks through Phase 1, 2, 4, 6.3, and 7.8 in one run when invoked with no `--phase`; it does not walk through Phases 1–7 in full, and nothing else does either.

### Mac Workstations

```bash
# Homebrew (if not installed)
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

brew install --cask ollama
brew install python@3.11 node go rustup-init jq
rustup-init -y && source "$HOME/.cargo/env"
brew install dotnet-sdk   # for dede, ai-coding-factory
brew install asitop       # GPU monitoring

ollama serve &  # or use the Ollama app
ollama pull llama3.2:3b
ollama pull nomic-embed-text
```

### Jetson Nodes

```bash
ssh nvidia@10.0.2.10

curl -fsSL https://ollama.com/install.sh | sh
sudo systemctl enable --now ollama
ollama pull llama3.2:3b
ollama pull qwen3:0.6b
ollama pull nomic-embed-text

curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
source "$HOME/.cargo/env"   # for ain, aegis

# Python for oda-r — no version pin actually exists in that repo (no
# pyproject.toml python_requires, and the maintainer's own checked-in venv
# is 3.10.12) — whatever python3 ships with JetPack is fine.
sudo apt update && sudo apt install -y python3 python3-venv python3-pip
```

### OpenShift Cluster

Everything exposed from OpenShift in this guide uses Routes — DNS via the cluster's own wildcard domain (`*.apps.<cluster-domain>`), which is already a standard part of an OpenShift install, not something to work around. The one thing worth fixing regardless of that: this guide originally pushed custom images to `image-registry.openshift-image-registry.svc:5000` — that's cluster-internal Kubernetes DNS, unreachable from an external `docker push` no matter your DNS stance. The real external path is the registry's own Route, enabled once per cluster:

```bash
oc login --server=https://api.ocp.ai-platform.internal:6443

oc new-project ai-serving   --display-name="AI Model Serving"
oc new-project ai-gateways  --display-name="AI Gateways"
oc new-project ai-agents    --display-name="AI Agent Platform"

oc get pods -n nvidia-gpu-operator   # expect gpu-feature-discovery, nvidia-device-plugin
oc get nodes -l nvidia.com/gpu.present=true

# Expose the internal image registry externally (once per cluster):
oc patch configs.imageregistry.operator.openshift.io/cluster --type=merge \
  -p '{"spec":{"defaultRoute":true}}'
# Confirm the real hostname -- it's cluster-generated, don't assume it:
oc get route default-route -n openshift-image-registry -o jsonpath='{.spec.host}'
# Log Docker in against whatever that command prints:
oc registry login --to=/tmp/oc-auth.json
```

`docker tag`/`docker push`/`image:` references in Phases 1–3 below use `image-registry.openshift-image-registry.svc:5000` as a placeholder for the internal name — replace with the real Route hostname the command above printed before running them, and push via `docker --config /tmp push <that-hostname>/...` (using the auth file from `oc registry login`) rather than a bare `docker push`.

### Linux GPU Hosts

Check `gpu_vendor` in `inventory.yaml` first — the driver check differs by vendor; everything after that (conda env, deploy directory) is identical.

**NVIDIA** (`linux-gpu-01/02` — L40S/L4; `ocp-worker-gpu-*` if confirmed NVIDIA):

```bash
ssh admin@10.0.3.10
nvidia-smi
nvcc --version   # expect CUDA 12.4+
```

**AMD** (`gpu_vendor: amd` in inventory.yaml):

```bash
ssh admin@<amd-host-ip>
rocm-smi
/opt/rocm/bin/hipcc --version
```

If ROCm isn't installed:

```bash
# Adjust codename/version for your distro — check https://rocm.docs.amd.com
wget https://repo.radeon.com/amdgpu-install/latest/ubuntu/jammy/amdgpu-install_6.2.60204-1_all.deb
sudo apt install -y ./amdgpu-install_6.2.60204-1_all.deb
sudo amdgpu-install -y --usecase=rocm
sudo usermod -aG render,video $USER   # log out/in for group change
rocm-smi
```

**Intel Gaudi2** (`linux-gpu-03`): install the Habana driver + SynapseAI stack per Intel's own docs (`hl-smi` to verify) — standard Habana setup, outside this workspace. No repo here has a code path for it (see the [GPU matrix](#gpu-vendor-support-matrix)).

Don't use `oda.sh` for GPU driver setup on any of these — its GPU auto-detection is dead code, so its install step doesn't fire regardless of `--no-gpu`. Use it only for non-GPU environment setup and handle drivers as shown above.

**Both vendors — common environment setup:**

```bash
wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh
bash Miniconda3-latest-Linux-x86_64.sh -b
eval "$($HOME/miniconda3/bin/conda shell.bash hook)"

conda create -n ai-platform python=3.11 -y
conda activate ai-platform

sudo mkdir -p /opt/ai-platform && sudo chown admin:admin /opt/ai-platform

# vllm-turboquant (1.1) needs Docker on this host -- not installed by the
# ROCm/CUDA steps above. Without the group membership, every `docker`
# command here fails with "permission denied ... docker.sock" unless
# prefixed with sudo (and even sudo alone won't fix a missing install).
sudo apt update && sudo apt install -y docker.io
sudo systemctl enable --now docker
sudo usermod -aG docker $USER   # log out/in (or `newgrp docker`) for this to take effect
```

### Linux CPU Hosts

```bash
ssh admin@10.0.4.10

sudo apt update && sudo apt install -y \
  python3.11 python3.11-venv python3-pip \
  golang-go nodejs npm \
  qemu-kvm libvirt-daemon-system \
  debootstrap iptables jq curl \
  docker.io
sudo systemctl enable --now docker
sudo usermod -aG docker admin   # log out/in for group change; needed for Envoy (Phase 2.1)

curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
source "$HOME/.cargo/env"

ls -la /dev/kvm   # should exist — needed for firecracker-agentfs

sudo mkdir -p /opt/ai-platform && sudo chown admin:admin /opt/ai-platform
```

---

## Deployment Helper Script

Save on your Mac to push repos to remote hosts:

```bash
#!/usr/bin/env bash
# deploy.sh — Push a repo to a remote host and run a setup command
# Usage: ./deploy.sh <repo-name> <user@host> [setup-command]

set -euo pipefail

REPO_ROOT="/Users/laurianlamba/Gitlab/LocalProjects/mitko/mitkox-repos"
REMOTE_BASE="/opt/ai-platform"

REPO="$1"
TARGET="$2"
SETUP_CMD="${3:-echo 'No setup command specified'}"

echo "▸ Syncing ${REPO} → ${TARGET}:${REMOTE_BASE}/${REPO}"
rsync -avz --delete \
  --exclude '.git' --exclude 'node_modules' --exclude '__pycache__' \
  --exclude '.venv' --exclude 'target' \
  "${REPO_ROOT}/${REPO}/" "${TARGET}:${REMOTE_BASE}/${REPO}/"

echo "▸ Running setup on ${TARGET}"
ssh "${TARGET}" "cd ${REMOTE_BASE}/${REPO} && ${SETUP_CMD}"

echo "✓ Done: ${REPO} deployed to ${TARGET}"
```

```bash
chmod +x deploy.sh
```

---

## Automated Deployment (Human-in-the-Loop)

Two scripts at the repo root automate parts of the phases below — built strictly from the same commands documented in each phase section, not a separate implementation. If the two ever disagree, the phase sections are the source of truth.

**`generate-env.py`** — reads `inventory.yaml`, writes `.env.platform` with every resource's real connection URL (Envoy, rlmgw, each Jetson's Ollama, each vllm-turboquant-bearing host) derived once instead of hand-copied into every tool's config. It never touches a secret — bearer tokens/API keys aren't in `inventory.yaml` to begin with (see [Where Configuration Actually Lives](#where-configuration-actually-lives)), so those lines come out as clearly marked placeholders. Read-only against `inventory.yaml`; it only ever writes `.env.platform`.

```bash
pip3 install pyyaml
python3 generate-env.py
# writes .env.platform -- add it to .gitignore before it has real tokens in it
```

**`deploy-platform.py`** — the actual orchestrator, with a hard scope boundary stated plainly rather than blurred:

- **Genuinely automated** (runs real build/SSH/deploy commands, with a confirmation prompt before anything that builds an image, restarts a shared service, or targets a host whose `inventory.yaml` entry is still a placeholder): Phase 1 (vllm-turboquant, ds4-zgx-gb10, Ollama binding), Phase 2 (Envoy — including *generating* `envoy.yaml`'s backend list from whatever `inventory.yaml` currently says runs where, so adding or removing a GPU host and re-running this regenerates the routing instead of hand-editing YAML; rlmgw), Phase 4 (aegis), Phase 6.3 (ain-node), Phase 7.8 (local-harness's lane wiring).
- **Guided checklist only** — prints each real command, asks you to confirm before moving to the next, executes nothing on its own: Phase 3.1 (fabrica — Kata-operator cluster prep is a cluster-wide decision, not something a script should make silently), Phase 5.1/5.2 (SDFT/sparser-faster-llms — these are real, resource-consuming training runs; starting one is a decision you make deliberately, not a side effect of running a deploy script), Phase 6.1 (oda — genuinely can't be scripted, it's fully interactive with no working non-interactive flags), Phase 7.5 (omarchy-ai — interactive installer with a reboot in the middle).
- **Not covered at all**: the remaining Phase 3/6/7 components. They're simple enough (a handful of flags, no per-host fleet iteration) that the phase section itself is the fastest path — run `--list` for the current, authoritative breakdown rather than trusting this paragraph as it ages.

```bash
python3 deploy-platform.py --list              # see exactly what's automated vs. guided, right now
python3 deploy-platform.py                      # interactive menu, runs every automated phase in order
python3 deploy-platform.py --phase 2.1          # just Envoy
python3 deploy-platform.py --phase 3.1          # guided checklist for fabrica
```

Every automated step refuses to run against a placeholder value (`TBD`, `0`, anything containing `REPLACE`) still sitting in `inventory.yaml` for the target host, and checks SSH reachability before doing anything else — the two failure modes this exists specifically to catch. `--yes` skips every confirmation prompt; it exists for re-running a phase you already reviewed interactively (e.g. after an `inventory.yaml` edit), not as a way to avoid reading what it's about to do the first time.

---

## Phase 1 — Model Serving

**Goal:** an OpenAI-compatible endpoint on each tier of the fleet. **Depends on:** nothing but the target hardware. **Skippable:** no — this is what every other phase ultimately talks to.

### 1.1 — vllm-turboquant → OpenShift (NVIDIA) or any ROCm host (AMD)

TurboQuant's KV-cache quantization — this fork's actual differentiator over stock vLLM — only activates on NVIDIA **RTX A6000/SM86** or **GB10/SM121** (confirmed in `docs/features/quantization/turboquant_a6000.md`). The real OpenShift GPU nodes are L4/L40S (SM89) or possibly AMD — neither qualifies, so this is "deploy vLLM 0.19," not a throughput upgrade, unless an A6000 gets added. Both `docker/Dockerfile` (CUDA) and `docker/Dockerfile.rocm` (ROCm) are real — pick the one matching `gpu_vendor` in `inventory.yaml`. If a node turns out to be Gaudi2, skip vllm-turboquant there entirely.

**If you cloned this platform repo with submodules:** `vllm-turboquant/.git` is a small pointer file (`gitdir: ../.git/modules/vllm-turboquant`), not a real git directory — normal for a submodule, but `docker build .` run from inside it only sends that directory as build context, so the pointer's actual target never reaches the image. vLLM's `setup.py` derives its version from git history via `setuptools_scm`; with no usable git metadata inside the container it fails with `setuptools-scm was unable to detect version`. Fix, once, right after cloning — no separate clone needed, this stays inside the one checkout:

```bash
./fix-submodules-git.sh
```

This materializes a real, self-contained `.git` directory inside every submodule (each copied from the superproject's `.git/modules/<name>`, with the now-incorrect `core.worktree` back-reference stripped) — not just `vllm-turboquant`, since nothing rules out another of the 26 components hitting the same class of problem later. Safe to re-run — a no-op for whatever's already done. After this, `docker build .` from inside `vllm-turboquant/` works normally. `deploy-platform.py`'s own Phase 1.1 automation doesn't need this — it transfers a `git bundle` to the remote host instead of a plain rsync, so the remote build always gets real git history regardless of how the local copy was checked out.

**Build.** No registry needed for this step, on either vendor — the image only needs to exist in the local Docker daemon on whichever host will actually run it (that's also all `deploy-platform.py`'s Phase 1.1 automation ever does: build and run in place over SSH, nothing pushed anywhere):

```bash
cd /Users/laurianlamba/Gitlab/LocalProjects/mitko/mitkox-repos/vllm-turboquant
```

*NVIDIA:*

```bash
docker build -t vllm-turboquant:cuda \
  --build-arg CUDA_VERSION=12.9.1 \
  --build-arg PYTHON_VERSION=3.12 \
  -f docker/Dockerfile .
```

*AMD/ROCm* (base image `rocm/vllm-dev:base`; set `PYTORCH_ROCM_ARCH` to your card's gfx target — run `rocminfo | grep gfx` to confirm, e.g. `gfx942` for MI300X, `gfx90a` for MI210/MI250):

```bash
docker build -t vllm-turboquant:rocm \
  --build-arg ARG_PYTORCH_ROCM_ARCH=gfx942 \
  -f docker/Dockerfile.rocm .
```

**Run it directly** — the common case for both vendors: neither `linux-gpu-01`/`02` (NVIDIA) nor `linux-gpu-04` (AMD) in `inventory.yaml` are OpenShift nodes, they're standalone Linux boxes, so most deployments stop here:

```bash
# NVIDIA:
docker run -d --name vllm-turboquant --gpus all -p 8000:8000 \
  vllm-turboquant:cuda \
  vllm serve <your-model-repo-or-path> --tensor-parallel-size 2 --host 0.0.0.0 --port 8000

# AMD/ROCm:
docker run -d --name vllm-turboquant \
  --device=/dev/kfd --device=/dev/dri \
  --group-add video --ipc=host --shm-size 16g \
  -p 8000:8000 \
  vllm-turboquant:rocm \
  vllm serve <your-model-repo-or-path> --tensor-parallel-size 2 --host 0.0.0.0 --port 8000
```

**Only if deploying to the real OpenShift GPU nodes** (`ocp-worker-gpu-01`/`02` in `inventory.yaml` — a separate, optional path, not the default one above) does this need a registry at all. Tag and push to your registry's real Route hostname (from Prerequisites → OpenShift Cluster — `image-registry.openshift-image-registry.svc:5000` below is cluster-internal DNS and won't resolve from outside the cluster; replace it):

```bash
docker tag vllm-turboquant:cuda \
  image-registry.openshift-image-registry.svc:5000/ai-serving/vllm-turboquant:cuda
docker --config /tmp push \
  image-registry.openshift-image-registry.svc:5000/ai-serving/vllm-turboquant:cuda
# AMD equivalent: same pattern, tag :rocm instead of :cuda.
```

**Deploy.** Entrypoint is the `vllm serve <model>` CLI. Real TurboQuant flags are `--kv-cache-dtype`, `--enable-turboquant`, `--turboquant-metadata-path`, and `--attention-backend TRITON_ATTN` (all four required together per the README's example) — meaningful only on A6000/GB10, omit on L4/L40S/AMD. The metadata file isn't shipped; generate it first:

```bash
python benchmarks/generate_turboquant_metadata.py \
  --target-model <model> --calibration-model <model> \
  --recipe turboquant35 --output turboquant_kv.json
```

Only the image tag, GPU resource key, and node selector differ by vendor:

```yaml
# file: openshift/vllm-turboquant-deployment.yaml
# NVIDIA shown; for AMD swap image -> :rocm, nvidia.com/gpu -> amd.com/gpu,
# add nodeSelector amd.com/gpu.family.gfx942: "true" (or your device-plugin's
# label), and drop VLLM_ATTENTION_BACKEND (CUDA-backend-specific env var).
apiVersion: apps/v1
kind: Deployment
metadata:
  name: vllm-turboquant
  namespace: ai-serving
  labels:
    app: vllm-turboquant
spec:
  replicas: 2   # adjust to GPU count
  selector:
    matchLabels:
      app: vllm-turboquant
  template:
    metadata:
      labels:
        app: vllm-turboquant
    spec:
      containers:
        - name: vllm
          image: image-registry.openshift-image-registry.svc:5000/ai-serving/vllm-turboquant:cuda
          command: ["vllm", "serve"]
          args:
            - "<your-model-repo-or-path>"
            - "--tensor-parallel-size"
            - "2"
            - "--host"
            - "0.0.0.0"
            - "--port"
            - "8000"
            - "--max-model-len"
            - "32768"
          ports:
            - containerPort: 8000
              name: http
          resources:
            limits:
              nvidia.com/gpu: "2"
            requests:
              nvidia.com/gpu: "2"
              memory: "32Gi"   # host system RAM, not GPU VRAM — size to
                               # whatever ocp-worker-gpu-01/02 actually has
          readinessProbe:
            httpGet: { path: /health, port: 8000 }
            initialDelaySeconds: 120
            periodSeconds: 10
          livenessProbe:
            httpGet: { path: /health, port: 8000 }
            initialDelaySeconds: 180
            periodSeconds: 30
      nodeSelector:
        nvidia.com/gpu.present: "true"
---
apiVersion: v1
kind: Service
metadata:
  name: vllm-turboquant
  namespace: ai-serving
spec:
  selector:
    app: vllm-turboquant
  ports:
    - port: 8000
      targetPort: 8000
      name: http
---
apiVersion: route.openshift.io/v1
kind: Route
metadata:
  name: vllm-turboquant
  namespace: ai-serving
spec:
  to: { kind: Service, name: vllm-turboquant }
  port: { targetPort: http }
```

```bash
oc apply -f openshift/vllm-turboquant-deployment.yaml
oc rollout status deployment/vllm-turboquant -n ai-serving
curl -s http://vllm-turboquant-ai-serving.apps.ocp.ai-platform.internal/health
# Expected: {"status":"ok"}
```

If you ran it standalone instead (the common case, above), the health check is simpler — no `oc`, no Route:

```bash
curl -s http://<host>:8000/health
```

### 1.2 — ds4-zgx-gb10 ("DwarfStar") → Mac Workstations

⚠️ **Check your Mac's RAM before going further — the smallest model this project ships needs far more than a typical laptop has.** Confirmed via `download_model.sh --help`: the smallest downloadable variant, DeepSeek V4 Flash `q2-imatrix`, is **81GB on disk**, "recommended for 96 and 128GB RAM machines." Every other variant (Flash q4, PRO, GLM-5.2) is larger still — 153GB up to 434GB. On a Mac with 16–32GB of unified memory, `./download_model.sh` will pull a file that can't be loaded at all, or `ds4-server` will fail to start / get OOM-killed even if it downloads successfully. There is no smaller supported quant to fall back to. If your Mac fleet tops out well under ~96GB, skip this component entirely — see [Ollama](#phase-1--model-serving) (already part of Phase 1's Jetson/dev-sandbox tier) for a serving path that actually fits smaller hardware, or `vllm-turboquant` (Phase 1.1) if you have a qualifying GPU host instead.

A narrow, DeepSeek-V4/GLM-5.2-specific native inference engine (project name **DwarfStar**), not a generic runner. `make` on macOS builds five binaries: `ds4`, `ds4-server`, `ds4-bench`, `ds4-eval`, `ds4-agent`.

```bash
cd /Users/laurianlamba/Gitlab/LocalProjects/mitko/mitkox-repos/ds4-zgx-gb10
make

# Explicit subcommand required — not auto-detecting:
./download_model.sh --help
# e.g.: ./download_model.sh q4-imatrix          (DeepSeek V4 Flash, Q4)
#       ./download_model.sh pro-q4-layers00-30  (DeepSeek V4 PRO, Q4, part 1/2)
#       ./download_model.sh q2-imatrix          (smallest)
# Add --token <HF_TOKEN> if the repo requires auth.

./ds4 -m gguf/<downloaded-file>.gguf -p "Hello, world" -n 100

# Real server flags are --ctx / --kv-disk-dir / --kv-disk-space-mb / -m.
# --host 0.0.0.0 is required explicitly for LAN access (default is
# local-only); --cors only affects browser headers, not network exposure.
./ds4-server -m gguf/<downloaded-file>.gguf \
  --ctx 100000 \
  --kv-disk-dir /tmp/ds4-kv \
  --kv-disk-space-mb 8192 \
  --host 0.0.0.0 \
  --port 8080
```

Endpoints: `GET /v1/models`, `POST /v1/chat/completions`, `POST /v1/responses` (Codex-CLI-style clients), `POST /v1/completions`, `POST /v1/messages` (Claude-Code-compatible), plus model-alias routes `GET /v1/models/deepseek-v4-flash` and `GET /v1/models/deepseek-v4-pro`. Default port is `8000` per source, overridden to `8080` below for consistency with the rest of this platform.

**launchd service (auto-start on Mac):**

```bash
cat > ~/Library/LaunchAgents/com.ai-platform.ds4-server.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.ai-platform.ds4-server</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/laurianlamba/Gitlab/LocalProjects/mitko/mitkox-repos/ds4-zgx-gb10/ds4-server</string>
        <string>-m</string>
        <string>/Users/laurianlamba/Gitlab/LocalProjects/mitko/mitkox-repos/ds4-zgx-gb10/gguf/REPLACE-WITH-ACTUAL-FILENAME.gguf</string>
        <string>--ctx</string>
        <string>100000</string>
        <string>--kv-disk-dir</string>
        <string>/tmp/ds4-kv</string>
        <string>--kv-disk-space-mb</string>
        <string>8192</string>
        <string>--host</string>
        <string>0.0.0.0</string>
        <string>--port</string>
        <string>8080</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/ds4-server.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/ds4-server.err</string>
</dict>
</plist>
EOF

launchctl load ~/Library/LaunchAgents/com.ai-platform.ds4-server.plist
```

**Additional Mac workstations:**

```bash
./deploy.sh ds4-zgx-gb10 dev@10.0.1.11 "make && ./download_model.sh q4-imatrix"
# Then SSH in and set up the launchd plist there too.
```

**Health check:**

```bash
curl -s http://localhost:8080/v1/models | jq .
```

### 1.3 — Ollama on Jetsons (verify GPU acceleration)

⚠️ **Unverified risk, not a confirmed fact:** the official `ollama.com/install.sh` targets generic Linux; Jetson AGX boards use Tegra iGPUs via NVIDIA's L4T/JetPack stack, and mainline Ollama has a history of silently falling back to CPU on Tegra. Confirm with `ollama ps` (CPU/GPU split) and `tegrastats`/`jtop` while running a prompt before trusting these nodes. If it's CPU-only, look at `dusty-nv/jetson-containers`'s Ollama image (built against L4T CUDA) instead of the stock installer.

```bash
for JETSON_IP in 10.0.2.10 10.0.2.11 10.0.2.12 10.0.2.13 10.0.2.14; do
  echo "=== Checking jetson at ${JETSON_IP} ==="
  ssh nvidia@${JETSON_IP} "
    systemctl is-active ollama && echo 'Ollama: RUNNING' || echo 'Ollama: DOWN'
    ollama list
    ollama pull llama3.2:3b
    ollama pull qwen3:0.6b
    ollama pull nomic-embed-text
  "
done
```

**Bind to all interfaces:**

```bash
ssh nvidia@10.0.2.10 "
  sudo mkdir -p /etc/systemd/system/ollama.service.d
  sudo tee /etc/systemd/system/ollama.service.d/override.conf << 'EOF'
[Service]
Environment=\"OLLAMA_HOST=0.0.0.0\"
EOF
  sudo systemctl daemon-reload
  sudo systemctl restart ollama
"
```

**Verify GPU use, and health check:**

```bash
ssh nvidia@10.0.2.10 "ollama run llama3.2:3b 'hi' --verbose 2>&1 | tail -5; ollama ps"
curl -s http://10.0.2.10:11434/api/tags | jq '.models[].name'
```

---

## Phase 2 — Gateway

**Goal:** one endpoint that unifies every Phase 1 backend, and a second, narrower endpoint that injects codebase context for coding tools. **Depends on:** at least one Phase 1 backend to point at. **Skippable:** entirely — any tool downstream can talk to a Phase 1 backend directly instead of through either of these.

`rlmgw` was originally assumed to be a multi-backend router. It isn't: `RLMgwConfig` holds a single `upstream_base_url` — it's a context-injecting proxy in front of exactly one backend, nothing more. Nothing in the 25 repos does multi-backend unification, so Envoy is added to fill that gap specifically.

### 2.1 — Envoy → linux-cpu-01 (the unifying gateway)

Deploys onto `linux-cpu-01`, sharing the host with rlmgw (2.2), `ain-node`, `firecracker-agentfs`, and aegis — not a dedicated box. It runs there rather than inside OpenShift because it has to reach backends on three separate network segments (OpenShift's Route, a Mac, a Jetson) and a plain LAN host reaches all three without any extra cluster networking — see [Architecture](#architecture) for the full reasoning.

Envoy has no built-in concept of "route by the `model` field in a JSON body." Two ways to get that: **[Envoy AI Gateway](https://aigateway.envoyproxy.io/)** (a real, current, production-ready CNCF/Envoy-ecosystem project with `AIGatewayRoute`/`AIServiceBackend` CRDs purpose-built for this — the right answer if you want per-key budgets or multi-tenant quotas later, but it needs Envoy Gateway + Gateway API CRDs on the cluster, more infrastructure than a single-operator platform needs today), or **plain Envoy with a small Lua filter** that reads `model` from the body and sets a header Envoy's native routing matches on, with backend failover via Envoy's built-in **aggregate cluster** (a stable, native priority-failover primitive). This guide uses the second option.

This was built and tested live, not just written from docs: installed Envoy 1.39.0 locally, validated the config, then ran it against three stub backends. Confirmed: missing/wrong bearer token → 401; each model name routes to its own backend; killing the primary backend and waiting out the health-check window made traffic automatically reroute to the next priority tier with zero client-side retry logic.

**Trade-off:** the bearer token and model→backend map are baked into the Lua script text — changing either means re-rendering `envoy.yaml` and restarting, not a live reload, and there's no per-key usage tracking. If you need either, that's the trigger to revisit Envoy AI Gateway.

```bash
ssh admin@10.0.4.10 "mkdir -p /opt/ai-platform/envoy && cat > /opt/ai-platform/envoy/envoy.yaml << 'EOF'
admin:
  address:
    socket_address: { address: 127.0.0.1, port_value: 9901 }

static_resources:
  listeners:
    - name: llm_listener
      address:
        socket_address: { address: 0.0.0.0, port_value: 4000 }
      filter_chains:
        - filters:
            - name: envoy.filters.network.http_connection_manager
              typed_config:
                \"@type\": type.googleapis.com/envoy.extensions.filters.network.http_connection_manager.v3.HttpConnectionManager
                stat_prefix: llm_gw
                route_config:
                  name: llm_routes
                  virtual_hosts:
                    - name: llm
                      domains: [\"*\"]
                      routes:
                        - match:
                            prefix: \"/\"
                            headers:
                              - name: x-model-tier
                                string_match: { exact: \"edge\" }
                          route: { cluster: ollama_edge, timeout: 300s }
                        - match:
                            prefix: \"/\"
                            headers:
                              - name: x-model-tier
                                string_match: { exact: \"mac\" }
                          route: { cluster: ds4_mac, timeout: 300s }
                        # Default: production serving, with automatic failover.
                        - match: { prefix: \"/\" }
                          route:
                            cluster: production_with_failover
                            timeout: 300s
                            retry_policy:
                              retry_on: \"5xx,reset,connect-failure,refused-stream\"
                              num_retries: 2
                              host_selection_retry_max_attempts: 3
                http_filters:
                  - name: envoy.filters.http.lua
                    typed_config:
                      \"@type\": type.googleapis.com/envoy.extensions.filters.http.lua.v3.Lua
                      default_source_code:
                        inline_string: |
                          local VALID_TOKEN = \"REPLACE_WITH_REAL_TOKEN\"
                          local MODEL_TO_TIER = {
                            [\"fast-mac\"] = \"mac\",
                            [\"edge-jetson\"] = \"edge\",
                          }
                          function envoy_on_request(request_handle)
                            local auth = request_handle:headers():get(\"authorization\")
                            if auth ~= (\"Bearer \" .. VALID_TOKEN) then
                              request_handle:respond({[\":status\"] = \"401\"}, '{\"error\":\"invalid or missing bearer token\"}')
                              return
                            end
                            local body_handle = request_handle:body()
                            if body_handle then
                              local body_str = body_handle:getBytes(0, body_handle:length())
                              local model = body_str:match('\"model\"%s*:%s*\"([^\"]+)\"')
                              local tier = model and MODEL_TO_TIER[model]
                              if tier then
                                request_handle:headers():replace(\"x-model-tier\", tier)
                              end
                            end
                          end
                  - name: envoy.filters.http.router
                    typed_config:
                      \"@type\": type.googleapis.com/envoy.extensions.filters.http.router.v3.Router

  clusters:
    # Aggregate cluster: tries vllm-turboquant first, then ds4-server, then
    # Ollama — the fallback chain, driven by active health checks.
    - name: production_with_failover
      connect_timeout: 5s
      lb_policy: CLUSTER_PROVIDED
      cluster_type:
        name: envoy.clusters.aggregate
        typed_config:
          \"@type\": type.googleapis.com/envoy.extensions.clusters.aggregate.v3.ClusterConfig
          clusters: [vllm_openshift, ds4_mac, ollama_edge]

    - name: vllm_openshift
      connect_timeout: 5s
      type: STRICT_DNS   # Envoy runs OUTSIDE the OpenShift cluster (on
                          # linux-cpu-01), so it must resolve vllm-turboquant's
                          # externally-exposed Route hostname -- NOT the
                          # cluster-internal "vllm-turboquant.ai-serving.svc"
                          # DNS name, which only resolves from inside the
                          # cluster's pod network.
      lb_policy: ROUND_ROBIN
      health_checks:
        - timeout: 3s
          interval: 10s
          unhealthy_threshold: 3
          healthy_threshold: 2
          http_health_check: { path: \"/health\" }
      load_assignment:
        cluster_name: vllm_openshift
        endpoints:
          - lb_endpoints:
              - endpoint:
                  # Routes are always served externally on port 80/443 by the
                  # router, regardless of the backend Service's internal port.
                  address:
                    socket_address: { address: vllm-turboquant-ai-serving.apps.ocp.ai-platform.internal, port_value: 80 }

    - name: ds4_mac
      connect_timeout: 5s
      type: STATIC   # was STRICT_DNS -- address below is already a raw IP,
                      # STATIC skips Envoy's DNS-refresh machinery entirely
      lb_policy: ROUND_ROBIN
      health_checks:
        - timeout: 3s
          interval: 10s
          unhealthy_threshold: 3
          healthy_threshold: 2
          http_health_check: { path: \"/v1/models\" }
      load_assignment:
        cluster_name: ds4_mac
        endpoints:
          - lb_endpoints:
              - endpoint:
                  address:
                    socket_address: { address: 10.0.1.10, port_value: 8080 }

    - name: ollama_edge
      connect_timeout: 5s
      type: STATIC
      lb_policy: ROUND_ROBIN
      health_checks:
        - timeout: 3s
          interval: 10s
          unhealthy_threshold: 3
          healthy_threshold: 2
          http_health_check: { path: \"/api/tags\" }
      load_assignment:
        cluster_name: ollama_edge
        endpoints:
          - lb_endpoints:
              - endpoint:
                  address:
                    socket_address: { address: 10.0.2.10, port_value: 11434 }
EOF"

ssh admin@10.0.4.10 "sed -i 's/REPLACE_WITH_REAL_TOKEN/sk-REPLACE-ME/' /opt/ai-platform/envoy/envoy.yaml"
```

**systemd service (wraps `docker run`):**

```bash
ssh admin@10.0.4.10 "sudo tee /etc/systemd/system/envoy-gateway.service << 'EOF'
[Unit]
Description=Envoy - Unified Model Gateway
After=network.target docker.service
Requires=docker.service

[Service]
Type=simple
ExecStartPre=-/usr/bin/docker rm -f envoy-gateway
ExecStart=/usr/bin/docker run --rm --name envoy-gateway \
  -p 4000:4000 -p 127.0.0.1:9901:9901 \
  -v /opt/ai-platform/envoy/envoy.yaml:/etc/envoy/envoy.yaml:ro \
  envoyproxy/envoy:v1.39.0
ExecStop=/usr/bin/docker stop envoy-gateway
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now envoy-gateway
"
```

**Health check** — validate syntax before trusting a running instance:

```bash
docker run --rm -v /opt/ai-platform/envoy/envoy.yaml:/etc/envoy/envoy.yaml:ro \
  envoyproxy/envoy:v1.39.0 --mode validate -c /etc/envoy/envoy.yaml

ssh admin@10.0.4.10 "curl -s http://127.0.0.1:9901/clusters | grep health_flags"

curl -s http://10.0.4.10:4000/v1/chat/completions \
  -H "Authorization: Bearer sk-REPLACE-ME" -H "Content-Type: application/json" \
  -d '{"model":"production","messages":[{"role":"user","content":"hello"}],"max_tokens":50}' \
  | jq .choices[0].message.content
```

### Adding, changing, or removing a backend

If your GPU fleet changes over time (nodes added, swapped, retired), expect to do this regularly — it's a normal operating task here, not a one-time setup step. Three things in `envoy.yaml` change together, then validate and restart:

1. **Cluster block** — add, edit, or remove the backend's `- name: <cluster_name>` entry under `clusters:` (its endpoint address/port and health-check path).
2. **Route** — the matching `route` entry under `route_config.virtual_hosts[0].routes`. Named tiers (like `fast-mac`, `edge-jetson`) match on the `x-model-tier` header; anything else falls through to the default route, `production_with_failover`.
3. **Lua model map** — if the backend should be reachable by a specific model-name tier, add/edit/remove its entry in the Lua script's `MODEL_TO_TIER` table. If it's a member of the failover chain instead, add/remove it from `production_with_failover`'s `clusters: [...]` list — that list's order **is** the priority order.

Then, every time, regardless of which of the three changed:

```bash
# 1. Edit the file (directly on the host, or edit locally and scp it over):
ssh admin@10.0.4.10 "vi /opt/ai-platform/envoy/envoy.yaml"

# 2. Validate before touching the running instance:
ssh admin@10.0.4.10 "docker run --rm -v /opt/ai-platform/envoy/envoy.yaml:/etc/envoy/envoy.yaml:ro envoyproxy/envoy:v1.39.0 --mode validate -c /etc/envoy/envoy.yaml"

# 3. Restart to pick up the change:
ssh admin@10.0.4.10 "sudo systemctl restart envoy-gateway"
```

Expect a few seconds of dropped in-flight connections during the restart — this is a plain restart, not a hot reload (that needs Envoy's xDS control-plane API, which this setup deliberately doesn't run; see the design note earlier in this phase). That's a fine trade-off for a single-operator platform, not something worth building around.

**`inventory.yaml` doesn't update itself.** It isn't read by Envoy or anything else (see [Prerequisites](#prerequisites-by-host-type)) — removing or adding a host there has zero effect on what Envoy actually routes to, and vice versa. When the fleet changes, update both by hand, at the same time, or the file quietly stops reflecting reality.

### 2.2 — rlmgw → linux-cpu-01 (repo-context proxy, single backend)

Real port is **8010**. No console-script entry point — invoked as a module — and it needs the `gw` extra specifically (`.[dev]` alone won't install fastapi/uvicorn/pydantic).

Four things worth knowing before running this in production: it **rejects streaming requests** with a 400 (coding clients that default to streaming need it disabled for this endpoint); `repo_root` is **fixed at process startup**, not per-request — one instance per active repo is the only supported mode; `/readyz`'s upstream check hits `{upstream_base_url}/healthz`, which against a real vLLM backend (whose actual health path is `/health`, no `/v1` prefix) will likely always report unhealthy — don't alert on it without expecting false negatives; and `pyproject.toml` names the project `rlm` and only declares `rlm`/`rlm.*` to setuptools, so a non-editable/wheel build would silently drop the `rlmgw` package — stick to the editable install below.

```bash
./deploy.sh rlmgw admin@10.0.4.10 "
  python3.11 -m venv venv &&
  source venv/bin/activate &&
  pip install -e '.[gw]'
"
```

**Config is env-var driven** (`RLMGW_` prefix), not a self-read `.env` file, though systemd's `EnvironmentFile=` can supply one:

```bash
ssh admin@10.0.4.10 "cat > /opt/ai-platform/rlmgw/rlmgw.env << 'EOF'
RLMGW_HOST=0.0.0.0
RLMGW_PORT=8010
RLMGW_UPSTREAM_BASE_URL=http://vllm-turboquant-ai-serving.apps.ocp.ai-platform.internal/v1
RLMGW_UPSTREAM_MODEL=<your-model-name>
RLMGW_REPO_ROOT=/opt/ai-platform/active-repo
RLMGW_MAX_CONTEXT_PACK_CHARS=12000
RLMGW_SESSION_TTL_HOURS=24
EOF"
```

**systemd service:**

```bash
ssh admin@10.0.4.10 "sudo tee /etc/systemd/system/rlmgw.service << 'EOF'
[Unit]
Description=RLMgw - Repo-Context Proxy for Coding Workloads
After=network.target

[Service]
Type=simple
User=admin
WorkingDirectory=/opt/ai-platform/rlmgw
EnvironmentFile=/opt/ai-platform/rlmgw/rlmgw.env
Environment=PATH=/opt/ai-platform/rlmgw/venv/bin:/usr/local/bin:/usr/bin
ExecStart=/opt/ai-platform/rlmgw/venv/bin/python -m rlmgw.server --host \${RLMGW_HOST} --port \${RLMGW_PORT} --repo-root \${RLMGW_REPO_ROOT}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now rlmgw
"
```

**Health check** — real routes are `/healthz` and `/readyz` (bare `/` 404s):

```bash
curl -s http://10.0.4.10:8010/healthz | jq .
curl -s http://10.0.4.10:8010/readyz | jq .
```

### 2.3 — sonic → OpenShift

WebSocket agent gateway in front of vLLM. Default port **9000**, health path **`/healthz`**. Needs `VLLM_URL` pointed at exactly one vLLM backend — it doesn't talk to rlmgw or Envoy. No Dockerfile ships in the repo; build one rather than mounting the source tree via ConfigMap (would exceed the ~1MiB limit):

```bash
cd /Users/laurianlamba/Gitlab/LocalProjects/mitko/mitkox-repos/sonic
cat > Dockerfile << 'EOF'
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 9000
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9000"]
EOF

docker build -t sonic:latest .
docker tag sonic:latest image-registry.openshift-image-registry.svc:5000/ai-gateways/sonic:latest
# Replace with the real registry Route from Prerequisites -> OpenShift Cluster.
docker --config /tmp push image-registry.openshift-image-registry.svc:5000/ai-gateways/sonic:latest
```

```yaml
# file: openshift/sonic-deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: sonic
  namespace: ai-gateways
spec:
  replicas: 2
  selector:
    matchLabels:
      app: sonic
  template:
    metadata:
      labels:
        app: sonic
    spec:
      containers:
        - name: sonic
          image: image-registry.openshift-image-registry.svc:5000/ai-gateways/sonic:latest
          ports:
            - containerPort: 9000
          env:
            - name: VLLM_URL
              value: "http://vllm-turboquant.ai-serving.svc:8000"
            - name: MODEL_NAME
              value: "<your-model-name>"
            - name: STATE_DB_PATH
              value: "/data/sonic_state.db"
          volumeMounts:
            - name: sonic-data
              mountPath: /data
          readinessProbe:
            httpGet: { path: /healthz, port: 9000 }
            initialDelaySeconds: 30
      volumes:
        - name: sonic-data
          persistentVolumeClaim:
            claimName: sonic-data
---
apiVersion: v1
kind: Service
metadata:
  name: sonic
  namespace: ai-gateways
spec:
  selector:
    app: sonic
  ports:
    - port: 9000
      targetPort: 9000
```

```bash
oc create -n ai-gateways -f - <<EOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: sonic-data
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 10Gi
EOF

oc apply -f openshift/sonic-deployment.yaml
oc expose svc/sonic -n ai-gateways
curl -s http://sonic-ai-gateways.apps.ocp.ai-platform.internal/healthz
```

---

## Phase 3 — Agent Execution

**Goal:** sandboxed environments for autonomous coding agents. **Depends on:** Kubernetes+Kata (fabrica) or KVM (firecracker-agentfs); an endpoint from Phase 1/2 for the other two. **Skippable:** yes, all four independently — none of them depend on each other despite what an earlier draft of this guide assumed (see [Component Reference](#component-reference)).

### 3.1 — fabrica → OpenShift sandboxed containers

The real binary, built from `./cmd/sandbox-manager`, takes **CLI flags only** — there is no YAML config loader anywhere in the Go source. Flags: `-listen` (default `:8080`), `-db`, `-kubeconfig`, `-namespace`, `-runtimeclass`, `-workspace-base`, `-golden-path`, `-log-level`, `-reconcile-interval`, `-repo-url`, `-quota-max-sandboxes`, `-quota-max-cpu`, `-quota-max-memory`. There is no gRPC API. A real Helm chart exists at `deploy/helm/fabrica/` with `make helm-deploy-dev`/`make helm-deploy-prod`/`make helm-smoke` targets — use it rather than a hand-written Deployment.

Deploys via the OpenShift sandboxed-containers/Kata operator, not a bare host. Prerequisite, done once per cluster: install the `sandboxed-containers-operator` from OperatorHub, create a `KataConfig` targeting the worker nodes that will host sandboxes, confirm a `kata`/`kata-remote` `RuntimeClass` exists. This node prep is standard Kata-on-OpenShift work, not documented in fabrica's own repo — consult Red Hat's sandboxed-containers-operator docs for your OpenShift version.

```bash
# oc login already done in Prerequisites -> OpenShift Cluster.

# 1. Confirm the operator/KataConfig/RuntimeClass are in place:
oc get kataconfig
oc get runtimeclass kata kata-remote 2>/dev/null

# 2. Build and push the image
cd /Users/laurianlamba/Gitlab/LocalProjects/mitko/mitkox-repos/fabrica
docker build -t fabrica:latest .
docker tag fabrica:latest \
  image-registry.openshift-image-registry.svc:5000/ai-agents/fabrica:latest
# Replace with the real registry Route from Prerequisites -> OpenShift Cluster.
docker --config /tmp push image-registry.openshift-image-registry.svc:5000/ai-agents/fabrica:latest

# 3. Deploy via the repo's own Helm chart — review values-production.yaml
#    first and override image/registry/runtimeClassName to match your cluster.
#    Leaves service.type at the chart's default (ClusterIP) and exposes it
#    via a Route instead, same pattern as every other OpenShift service here.
helm upgrade --install fabrica deploy/helm/fabrica \
  -n ai-agents --create-namespace \
  -f deploy/helm/fabrica/values-production.yaml \
  --set image.repository=image-registry.openshift-image-registry.svc:5000/ai-agents/fabrica \
  --set image.tag=latest

# The chart has no Route template of its own -- add one:
oc create route edge fabrica -n ai-agents --service=fabrica --port=http 2>/dev/null \
  || oc expose svc/fabrica -n ai-agents --port=8080

make helm-smoke
```

**Health check:**

```bash
oc get pods -n ai-agents -l app=fabrica
curl -s http://fabrica-ai-agents.apps.ocp.ai-platform.internal/health
```

### 3.2 — firecracker-agentfs → linux-cpu-01

Builds Firecracker microVM boot artifacts. No `.ext4` file is ever produced — `build-rootfs.sh` produces a **live directory tree** at `./rootfs/` via `debootstrap`; an AgentFS overlay database (`.agentfs/<agent-id>.db`) serves it live over **NFSv3**, and Firecracker boots each microVM with `root=/dev/nfs`. There's no "build once, mount a file" step.

```bash
./deploy.sh firecracker-agentfs admin@10.0.4.10 "
  sudo -v &&
  ./build-kernel.sh &&
  ./build-rootfs.sh
"
# Both scripts call sudo internally for the privileged steps they need.

# Verify the real artifacts:
ssh admin@10.0.4.10 "ls -la /opt/ai-platform/firecracker-agentfs/rootfs/ | head; ls -la /opt/ai-platform/firecracker-agentfs/.agentfs/ 2>/dev/null"
```

This step alone is **not sufficient** to launch a sandbox — that also needs the host `agentfs` binary serving the NFS export and `firecracker.sh` (TAP device, NAT, launching the VM), neither installed here. It's also unrelated to fabrica (3.1) despite the surface-level similarity — fabrica uses Kata Containers + Cloud Hypervisor, this uses raw Firecracker + AgentFS-over-NFS, and nothing in fabrica's source references this repo's output.

### 3.3 — background-coding-agents → OpenShift

Framed by its own README as industrial PLC/SCADA migration automation modeled on Spotify's internal engineering approach. Real ASGI app is `background_coding_agents.api.app:app` (src-layout package). Port `8080` is `.env.example`'s documented default, but the code doesn't actually read `API_PORT`/`API_HOST` from env — confirm the bound port at runtime. It talks directly to an OpenAI-compatible endpoint via `LLM_BASE_URL` — no gateway concept, no fabrica integration (`SANDBOX_API_URL` doesn't exist in the code), and no database: job state is an in-process Python dict that does not survive a pod restart.

```yaml
# file: openshift/background-coding-agents.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: background-coding-agents
  namespace: ai-agents
spec:
  replicas: 1
  selector:
    matchLabels:
      app: background-coding-agents
  template:
    metadata:
      labels:
        app: background-coding-agents
    spec:
      containers:
        - name: fleet-manager
          image: python:3.11-slim
          command: ["sh", "-c"]
          args:
            - |
              pip install -e '.[dev]' &&
              uvicorn background_coding_agents.api.app:app --host 0.0.0.0 --port 8080
          workingDir: /app
          ports:
            - containerPort: 8080
          env:
            - name: LLM_BASE_URL
              value: "http://10.0.4.10:8010/v1"   # rlmgw, or Envoy :4000 for
                                                    # multi-backend routing
          volumeMounts: []
---
apiVersion: v1
kind: Service
metadata:
  name: background-coding-agents
  namespace: ai-agents
spec:
  # ADDED — the original manifest here defined a Deployment only and then
  # ran `oc expose svc/background-coding-agents`, which requires a Service
  # of that name to already exist. It never did; that command would have
  # failed regardless of how it's exposed. Adding the missing Service here.
  selector:
    app: background-coding-agents
  ports:
    - port: 8080
      targetPort: 8080
```

```bash
oc apply -f openshift/background-coding-agents.yaml
oc expose svc/background-coding-agents -n ai-agents
curl -s http://background-coding-agents-ai-agents.apps.ocp.ai-platform.internal/api/v1/health
```

Real API is under `/api/v1`: `/health`, `/providers`, `/sites`, `/sites/{site_name}`, `/migrations`, `POST /migrations/run`, `GET /migrations/jobs/{job_id}`, `GET /migrations/jobs`, `DELETE /migrations/jobs/{job_id}`, `POST /verify`. There is no `/tasks` endpoint — submitting work means a `MigrationRequest` (`migration_name` referencing a YAML under `fleet_manager/migrations/`, `dry_run`, `target_sites`, `provider`, `model`; see [API Examples](#api-examples)).

### 3.4 — ai-coding-factory → not a deployable service

This is a governance/scaffolding framework, not something you build and deploy. There's no root-level `.csproj`/`.sln`/Dockerfile — the only ones live under `templates/clean-architecture-solution/`, a scaffold using the literal placeholder name `ProjectName`, meant to be copied to start a *new* project. `oc new-build`/`docker build .` against the repo root finds nothing.

The real mechanism is the `opencode` CLI driving AI Scrum-team agent roles (product-owner, scrum-master, developer, qa, security, devops) that scaffold and govern other .NET projects. Its inference-endpoint config lives in `.opencode/opencode.json` and `.env` (`OPENCODE_BASE_URL`, `OPENCODE_API_KEY`, `OPENCODE_MODEL`) — not `LLM_ENDPOINT`, which doesn't exist anywhere in the repo.

```bash
cd /Users/laurianlamba/Gitlab/LocalProjects/mitko/mitkox-repos/ai-coding-factory

cat > .env << 'EOF'
OPENCODE_BASE_URL=http://10.0.4.10:8010/v1
OPENCODE_API_KEY=none
OPENCODE_MODEL=<your-model-name>
EOF

opencode run "@Program-Manager scaffold a new service from templates/clean-architecture-solution"
```

A deployed ASP.NET service comes from *using* this to scaffold one (which gets its own Dockerfile/CI from the template) — not from deploying this repo itself.

---

## Phase 4 — Security Pipeline

**Goal:** every code change audited, every package install signed. **Depends on:** an endpoint from Phase 1/2, all optional. **Skippable:** yes, each of the three independently.

### 4.1 — aegis → All Hosts

Real subcommands (`crates/aegis-cli/src/main.rs`): `doctor`, `apt {update|upgrade|install}`, `npm install`, `pip install`, `container pull`, `docker pull`, `podman pull`, `nuget install`, `vscode install`, `go get`, `cargo install`, `review`, `policy`. There's no `wrap` subcommand — real invocation is `aegis npm install <package> --plan`.

Every subcommand only ever **writes a plan** when called with `--plan`; `--apply` hits a "not yet implemented" path. The real production flow is `aegis <ecosystem> install <pkg> --plan` → `aegis review` → `aegis policy` → `aegisctl sign` → `aegisctl verify`/`aegisctl apply` (talking to the `aegisd` daemon over a Unix socket). A plain shell alias (`alias npm="aegis npm install"`) would silently stop `npm install` from installing anything — don't wire that up without the full chain behind it.

```bash
# NOT a universal binary: cargo build --release on macOS produces a
# single-arch binary for the host's native arch only.
cd /Users/laurianlamba/Gitlab/LocalProjects/mitko/mitkox-repos/aegis
cargo build --release
# crates/aegis-cli declares FOUR binaries: aegis, aegisctl, aegisd,
# aegis-reviewd. All four are needed for the sign/apply pipeline.
ls target/release/{aegis,aegisctl,aegisd,aegis-reviewd}

# Do NOT run packaging/install-native.sh on macOS — its own usage banner
# says "Install Aegis native LINUX service assets," hard-wired to
# systemctl/useradd/groupadd/systemd, none of which exist on macOS. On Mac
# you get the CLI (plan/review/policy) only; the daemon-based sign/apply
# pipeline needs a Linux host — see below.
sudo cp target/release/{aegis,aegisctl,aegisd,aegis-reviewd} /usr/local/bin/
aegis --version
```

Cross-compiling from macOS to Linux isn't a solved path here — no `.cargo/config.toml`, no `cross`/`cargo-zigbuild` setup. Build natively on each target instead:

```bash
# On a Linux x86_64 host, or a Jetson/other aarch64 host:
cargo build --release
```

**Deploy to Linux hosts:**

```bash
for HOST in 10.0.4.10 10.0.4.11 10.0.3.10 10.0.3.11 10.0.3.12 10.0.3.13; do
  scp target/release/{aegis,aegisctl,aegisd,aegis-reviewd} admin@${HOST}:/tmp/
  ssh admin@${HOST} "sudo mv /tmp/aegis* /usr/local/bin/ && sudo ./packaging/install-native.sh && aegis --version"
done
```

**Deploy to Jetsons** — per `inventory.yaml`, aegis is only assigned to `jetson-01/02/03`:

```bash
for JETSON_IP in 10.0.2.10 10.0.2.11 10.0.2.12; do
  scp target/release/{aegis,aegisctl,aegisd,aegis-reviewd} nvidia@${JETSON_IP}:/tmp/
  ssh nvidia@${JETSON_IP} "sudo mv /tmp/aegis* /usr/local/bin/ && cd /opt/ai-platform/aegis && sudo ./packaging/install-native.sh && aegis --version"
done
```

The repo ships a real wrapper mechanism, `packaging/wrappers/aegis-package-wrapper` (symlink it as `npm`/`pip`/`apt`/etc. ahead of the real tools in `PATH`), but it's not referenced by the README or install script — treat it as unofficial, and remember even wrapped installs stop at "plan written" until run through `review`/`policy`/`aegisctl sign`/`aegisctl apply`.

### 4.2 — megacode → CI Pipeline / Mac

Console script `security-audit` (from `security-audit = "audit:main"`), real flags `--source-root`, `--output-report`/`--output-metadata`/`--output-manifest`. All configuration is CLI flags or `AUDIT_*` env vars — `AUDIT_SOURCE_ROOT`, `AUDIT_LM_MODEL` (default `openai/mitko`), `AUDIT_LM_API_BASE` (default `http://localhost:8000/v1`), `AUDIT_MAX_ITERATIONS`. There's no `megacode` Python package and no YAML config support.

```bash
cd /Users/laurianlamba/Gitlab/LocalProjects/mitko/mitkox-repos/megacode
pip install -e '.[dev]'

export AUDIT_LM_API_BASE=http://10.0.4.10:8010/v1
export AUDIT_LM_MODEL=<your-model-name>
security-audit --source-root /path/to/your/dotnet/project \
  --output-report ./security-reports/report.md
```

**GitLab CI:**

```yaml
security-audit:
  stage: test
  image: python:3.11
  before_script:
    - pip install /path/to/megacode
  script:
    - security-audit --source-root . --output-report security-report.md
  artifacts:
    paths:
      - security-report.md
    when: always
  variables:
    AUDIT_LM_API_BASE: http://10.0.4.10:8010/v1
```

### 4.3 — tnt → CI Pipeline / Mac

Pairs a fast discovery model ("Roadrunner") with a deeper triage model ("Coyote"); explicitly does not generate exploits.

```bash
cd /Users/laurianlamba/Gitlab/LocalProjects/mitko/mitkox-repos/tnt

# Python side depends on `rlms` via a uv-specific git source — plain pip
# won't resolve it from PyPI. Requires Python >=3.12.
cd python && uv sync && cd ..

# Node side requires Node >=20:
cd node && ./scripts/ensure-node20.sh && npm install && cd ..

cat > .env << 'EOF'
ROADRUNNER_ENDPOINT=http://10.0.4.10:8010/v1
ROADRUNNER_MODEL=<your-fast-model-name>

COYOTE_ENDPOINT=http://10.0.4.10:8010/v1
COYOTE_MODEL=<your-reasoning-model-name>
EOF

# Real one-shot scan+triage entrypoint (no `make triage` target exists):
./scripts/verify-harness.sh --repo-root /path/to/project
# `make verify` is equivalent. Manual two-step: `uv run --directory python
# security-harness scan --repo-root /path/to/project --mode current ...`
# then `npm --prefix node run -s cli -- triage-report --report <report.json>`.
```

---

## Phase 5 — Model Optimization

**Goal:** smaller/faster models for the edge tier. **Depends on:** a CUDA GPU (L4/L40S only — see the [GPU matrix](#gpu-vendor-support-matrix); skip on AMD and Gaudi2, nothing here runs on either). **Skippable:** yes — Phase 6 works fine with stock Ollama models instead.

### 5.1 — SDFT (Self-Distillation) → linux-gpu-01 (L40S)

Distills a large model (external teacher via vLLM) into a small model (student for Jetsons). Real entrypoint is a plain `python3 main.py` CLI via `HfArgumentParser((RunConfig, DistilConfig))` — flags directly, no `--config <file>.yaml` (no such flag exists; `distil_config.py` is a Python dataclass module). The README's teacher example is `GLM-4.7 30B MoE`; student is `Qwen/Qwen3-0.6B`.

```bash
# requirements.txt has no torch entry — install it explicitly, stable
# channel matching this host's CUDA (12.x), NOT the repo's own nightly-cu131
# instructions (those target NVIDIA GB10, absent from this fleet):
./deploy.sh SDFT admin@10.0.3.10 "
  conda activate ai-platform &&
  pip install torch --index-url https://download.pytorch.org/whl/cu124 &&
  pip install -r requirements.txt
"
# Treat this as an adaptation, not confirmed-working — watch for
# CUDA-capability errors on first run and be ready to try a different
# stable cu12x index.

# Start the external-teacher vLLM server (any OpenAI-compatible /v1 works —
# this platform's own vllm-turboquant deployment is fine; SDFT's design
# offloads the teacher so the training GPU only needs to hold the student):
#   vllm serve <teacher-model> --port 8000 --served-model-name <name>

ssh admin@10.0.3.10 "
  cd /opt/ai-platform/SDFT &&
  conda activate ai-platform &&
  python3 main.py \
    --output_dir /opt/ai-platform/models/qwen3-0.6b-distilled \
    --model_name_or_path Qwen/Qwen3-0.6B \
    --vllm_server_base_url http://vllm-turboquant-ai-serving.apps.ocp.ai-platform.internal/v1
"
```

No GGUF-conversion tooling ships in this repo (`SDFT/scripts/` has only `check_env.py`) — converting the distilled checkpoint for Ollama on the Jetsons means using `llama.cpp`'s own `convert_hf_to_gguf.py` (separate tool, not part of this workspace) against the output directory above.

### 5.2 — sparser-faster-llms → linux-gpu-01/02 (L40S/L4)

⚠️ **Possible hardware blocker, not just a config fix.** Its custom CUDA kernels (TwELL packing format) are documented as "designed for H100 GPUs" (Hopper, SM90); L40S/L4 are Ada (SM89) — this may be a genuine architecture mismatch. Confirm by trying the build and watching for compile/PTX errors before assuming it'll run.

```bash
./deploy.sh sparser-faster-llms admin@10.0.3.10 "
  conda activate ai-platform &&
  bash scripts/install.sh --full &&
  python scripts/check_gb10_cuda.py --build-twell
"
# scripts/install.sh is the real install path (requirements.txt alone has no
# torch entry and skips the TwELL kernel compile/check step). If
# check_gb10_cuda.py --build-twell fails, that's the Hopper-vs-Ada mismatch
# above, not something to work around.

# Launch syntax is POSITIONAL, not --model/--sparsity/--output flags, and
# trains one of four fixed architectures from cfgs/run_cfg/, not an
# arbitrary HF model:
ssh admin@10.0.3.10 "
  cd /opt/ai-platform/sparser-faster-llms &&
  conda activate ai-platform &&
  ./launch.sh 2 sparsity_gated_1p5b zero1
"
# The repo's own H100 example uses 8 GPUs (./launch.sh 8 sparsity_gated_1p5b
# zero1) — dropping to fewer GPUs may need more than changing this number
# (batch size / zero1 sharding may assume 8-way). Treat the first run as a
# smoke test.
```

### 5.3 — Thinking-with-Visual-Primitives → nothing to deploy

Ships a paper (PDF) and license files only — zero `.py` files anywhere in the repo, confirmed via `git ls-files`. Its `pyproject.toml`/`Makefile` are stale files copied from an unrelated DeepSeek project ("Janus") and reference nothing that exists here. Nothing to build, install, or run yet.

---

## Phase 6 — Edge Agents

**Goal:** autonomous agents on Jetsons, connected via P2P mesh. **Depends on:** a Jetson (oda, oda-r); nothing (ain runs anywhere, including Mac/Linux hosts). **Skippable:** yes, each of the three independently.

### 6.1 — oda → Jetson nodes (interactive session required)

`oda.sh`'s GPU auto-detection exists but is never called anywhere in `main()` — `--no-gpu` (a real flag) is currently a no-op either way. Only 3 of 5 documented CLI flags actually parse (`-h`/`--help`, `--no-gpu`, `--verbose`); `-y`/`--dry-run`/`--resume` are documented but not implemented. `main()` runs 9 blocking `read -p` prompts with no bypass — this is a real upstream limitation, not something this guide can script around.

```bash
# Stages the script only — does not run the installer, which cannot
# complete non-interactively as currently written:
for JETSON_IP in 10.0.2.10 10.0.2.11 10.0.2.12; do
  ./deploy.sh oda nvidia@${JETSON_IP} "chmod +x oda.sh"
done

# Actual install requires an interactive TTY, one host at a time:
ssh -t nvidia@10.0.2.10 "cd /opt/ai-platform/oda && ./oda.sh"
```

### 6.2 — oda-r → Jetson nodes

`CompilerConfig.from_yaml()` constructs the dataclass directly from parsed YAML — any unknown key raises `TypeError`, caught by a broad `except` that **silently falls back to defaults** (`server_url="http://localhost:8080/completion"`). Real fields: `server_url, max_tokens, temperature, top_p, top_k, repeat_penalty, presence_penalty, frequency_penalty, max_iterations, timeout, batch_size, connection_limit, max_retries` — no `model` field, no `metrics` section. Despite `requirements.txt` listing `dspy-ai`, `odar.py` never imports `dspy` — it's a hand-rolled async HTTP retry loop, not DSPy's Signatures/Modules.

```bash
for JETSON_IP in 10.0.2.10 10.0.2.11 10.0.2.12 10.0.2.13 10.0.2.14; do
  ./deploy.sh oda-r nvidia@${JETSON_IP} "
    python3 -m venv venv &&
    source venv/bin/activate &&
    pip install -r requirements.txt
  "

  ssh nvidia@${JETSON_IP} "cat > /opt/ai-platform/oda-r/config.yaml << 'EOF'
server_url: http://localhost:11434/v1/completions
max_retries: 3
timeout: 60
max_iterations: 5
batch_size: 1
connection_limit: 10
EOF"
done
```

### 6.3 — ain (P2P Agent Mesh) → Everywhere

No binary is actually named `ain` — the workspace produces `ain-cli` (subcommands: `keygen`, `sim-smoke` only — cannot run the mesh) and `ain-node` (the real daemon, HTTP API on **8787**, P2P on **4001**). `ain`'s own `SECURITY.md` flags `/v1/publish` as high-risk (sends `secret_key_b64` to the node in plaintext); this platform uses **local client-side signing via `/v1/events`** instead — each agent runs `ain-cli keygen` locally, keeps the secret key on its own machine, and builds/signs its own `EventEnvelope` before POSTing it already-signed. The `ain-sdk` crate is where that signing logic belongs — read its source directly before wiring an agent to it; this guide confirms the crate's purpose but not its exact API surface.

```bash
cd /Users/laurianlamba/Gitlab/LocalProjects/mitko/mitkox-repos/ain
cargo build --release
ls target/release/{ain-cli,ain-node}
# Cross-compiling from macOS has the same unaddressed linker gap as aegis
# (Phase 4.1) — build natively per-arch instead.
```

**Deploy `ain-node` to Linux hosts** (real flags: `--http-listen`, `--p2p-listen <multiaddr>`, `--bootstrap <multiaddr>` repeated per peer — not `--listen`/`--api-port`/`--seed-nodes`, and `--bootstrap` needs a full multiaddr including `/p2p/<peer-id>`, not a bare address):

```bash
for HOST in 10.0.4.10 10.0.4.11 10.0.3.10 10.0.3.11 10.0.3.12 10.0.3.13; do
  scp target/release/ain-node admin@${HOST}:/usr/local/bin/ain-node
  ssh admin@${HOST} "sudo tee /etc/systemd/system/ain-node.service << 'UNIT'
[Unit]
Description=AIN Node - Decentralized Agent Mesh
After=network.target

[Service]
Type=simple
User=admin
ExecStart=/usr/local/bin/ain-node \
  --http-listen 0.0.0.0:8787 \
  --p2p-listen /ip4/0.0.0.0/tcp/4001 \
  --bootstrap /ip4/10.0.4.10/tcp/4001/p2p/<bootstrap-peer-id> \
  --data-dir /opt/ai-platform/ain-data
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT
  sudo systemctl daemon-reload
  sudo systemctl enable --now ain-node
  "
done
```

**Deploy to Jetsons (built natively on aarch64):**

```bash
for JETSON_IP in 10.0.2.10 10.0.2.11 10.0.2.12 10.0.2.13 10.0.2.14; do
  scp target/release/ain-node nvidia@${JETSON_IP}:/tmp/ain-node
  ssh nvidia@${JETSON_IP} "
    sudo mv /tmp/ain-node /usr/local/bin/ain-node
    sudo tee /etc/systemd/system/ain-node.service << 'UNIT'
[Unit]
Description=AIN Node - Edge Agent Mesh
After=network.target ollama.service

[Service]
Type=simple
User=nvidia
ExecStart=/usr/local/bin/ain-node \
  --http-listen 0.0.0.0:8787 \
  --p2p-listen /ip4/0.0.0.0/tcp/4001 \
  --bootstrap /ip4/10.0.4.10/tcp/4001/p2p/<bootstrap-peer-id> \
  --data-dir /opt/ai-platform/ain-data
Restart=always

[Install]
WantedBy=multi-user.target
UNIT
    sudo systemctl daemon-reload
    sudo systemctl enable --now ain-node
  "
done
```

**Mac (launchd):**

```bash
cat > ~/Library/LaunchAgents/com.ai-platform.ain-node.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.ai-platform.ain-node</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/ain-node</string>
        <string>--http-listen</string>
        <string>0.0.0.0:8787</string>
        <string>--p2p-listen</string>
        <string>/ip4/0.0.0.0/tcp/4001</string>
        <string>--bootstrap</string>
        <string>/ip4/10.0.4.10/tcp/4001/p2p/&lt;bootstrap-peer-id&gt;</string>
        <string>--data-dir</string>
        <string>/Users/laurianlamba/.ain</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/ain-node.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/ain-node.err</string>
</dict>
</plist>
EOF

launchctl load ~/Library/LaunchAgents/com.ai-platform.ain-node.plist
```

**Verify mesh** — real route is `/v1/node/info`, not `/status`; there's no `/broadcast`:

```bash
curl -s http://localhost:8787/v1/node/info | jq '.p2p'

# Publishing a message means signing an EventEnvelope client-side (via
# ain-sdk) and POSTing it already-signed:
curl -s -X POST http://localhost:8787/v1/events \
  -H "Content-Type: application/json" -d @signed-event.json
```

---

## Phase 7 — Developer Tools

**Goal:** daily-use tooling. **Depends on:** varies per tool, see each subsection. **Skippable:** yes, each independently — none of it serves other phases.

### 7.1 — SkillOpt

Real entrypoints are `scripts/train.py --config <cfg> --cfg-options ...` and `scripts/eval_only.py --config <cfg> --skill <path> --split <name>` (or console scripts `skillopt-train`/`skillopt-eval`) — not `python3 -m skillopt optimize`, which doesn't exist. Config sections are `{model, train, gradient, optimizer, evaluation, env}`, not `{llm, optimizer}`.

```bash
cd /Users/laurianlamba/Gitlab/LocalProjects/mitko/mitkox-repos/SkillOpt
pip install -e .   # '.[dev]' only pulls lint/test tooling, not what you
                    # need to run optimization

cat > configs/local_mitko.yaml << 'EOF'
model:
  backend: openai_compat
  openai_compat_base_url: http://10.0.4.10:8010/v1
  openai_compat_model: <your-model-name>
optimizer:
  learning_rate: 0.1
EOF
# Start from configs/dotnetdebug/local_mitko.yaml as a template — it
# already uses the real key names.

# The bundled "DotNetDebug" example needs data/dotnetdebug/tasks.json
# generated first — it's gitignored, not actually bundled despite the README.

python3 scripts/train.py --config configs/local_mitko.yaml \
  --cfg-options skill_path=path/to/your/SKILL.md
```

### 7.2 — dede (.NET Dependency Explorer)

Real flow is two steps — `scan` then `serve` — not a single `--workspace`/`--port` invocation.

```bash
cd /Users/laurianlamba/Gitlab/LocalProjects/mitko/mitkox-repos/dede
dotnet build

dotnet run --project src/DogEatDog.DependencyExplorer.Cli -- \
  scan /path/to/your/dotnet/solution -o ./graph.json

dotnet run --project src/DogEatDog.DependencyExplorer.Cli -- \
  serve ./graph.json --url http://127.0.0.1:5057

open http://localhost:5057
```

### 7.3 — ccar (Autonomous Benchmarking)

Genuinely Claude Code slash commands + hooks, not a binary. `install-global.sh` copies its runtime to `~/.claude/autoresearch`, installs commands, patches `~/.claude/settings.json` (after its own timestamped backup). `jq` is a hard prerequisite.

```bash
cd /Users/laurianlamba/Gitlab/LocalProjects/mitko/mitkox-repos/ccar
./install-global.sh
```

Only works from inside a Claude Code session — type the slash command directly in chat:

```
/autoresearch <your goal> --max-iterations 20
```

That creates `autoresearch.md` (goal), `autoresearch.sh` (benchmark script), and `autoresearch.jsonl` (iteration log), then continues autonomously via the installed `Stop` hook.

### 7.4 — llama3.2-coreml (export only — no inference path in this repo)

`export_model.py` is a pure library module with no CLI. `run_model.py` is actually the **export/quantize CLI** (flags: `--output-dir`, `--skip-quantization`) — there's no `--prompt` flag and no inference/prompt-running code anywhere in the repo.

```bash
cd /Users/laurianlamba/Gitlab/LocalProjects/mitko/mitkox-repos/llama3.2-coreml
pip install torch coremltools transformers

python3 run_model.py --output-dir ./export
# Nothing further to run here — a separate Swift/Core ML host app would be
# needed to actually run inference against export/*.mlpackage.
```

### 7.5 — omarchy-ai (one-time interactive install, not a remote push)

Hard-requires a fresh Arch Linux install, clones upstream `basecamp/omarchy`, and its flow includes real interactive prompts ending in `sudo reboot` — doesn't fit a non-interactive SSH push. Real one-command entrypoint is `boot.sh`, not `install.sh`.

```bash
# Run manually, locally, on the fresh Arch box being provisioned:
curl -fsSL https://raw.githubusercontent.com/mitkox/omarchy-ai/main/boot.sh | bash
```

### 7.6 — fteplusai (nothing to deploy)

18 agent-definition files (`agents/*.agent.md`) and 23 skill files (`skills/*.skill.md`) meant to be loaded as context inside an AI coding assistant (`@Program-Manager` style invocation) — `index.html` is marketing collateral, not the deliverable.

```bash
ls /Users/laurianlamba/Gitlab/LocalProjects/mitko/mitkox-repos/fteplusai/agents/
ls /Users/laurianlamba/Gitlab/LocalProjects/mitko/mitkox-repos/fteplusai/skills/
```

### 7.7 — aimatch

Real interface is a `run`/`eval` CLI over JSONL files — `aimatch/__init__.py` exports `AIMatchConfig`, `AIMatchPipeline`, `MatchPrediction`, `RawProfile`; there is no `match()` function.

```bash
cd /Users/laurianlamba/Gitlab/LocalProjects/mitko/mitkox-repos/aimatch
pip install -e .   # zero external dependencies, confirmed

aimatch run --queries examples/synthetic/queries.jsonl \
  --candidates examples/synthetic/candidates.jsonl \
  --output /tmp/predictions.jsonl \
  --ground-truth examples/synthetic/ground_truth.jsonl \
  --calibration swiss

aimatch eval --predictions /tmp/predictions.jsonl \
  --ground-truth examples/synthetic/ground_truth.jsonl \
  --target-precision 0.95

# Optional LLM-backed reasoning instead of the default heuristic:
aimatch run --queries ... --candidates ... --output ... \
  --use-local-llm --llm-base-url http://10.0.4.10:8010/v1 --llm-model <your-model-name>
```

### 7.8 — local-harness

Not a mitkox repo — `github.com/aidotse/local-harness`, added separately. A personal, `127.0.0.1`-only gateway that runs on a developer's own Mac and gives one endpoint to tools like VS Code/Copilot Chat, Claude Code, or OpenCode, switchable (via an admin GUI) between a Claude subscription (spawns the real `claude` CLI), a Gemini subscription (spawns `gemini`), GitHub Copilot (via a bridge), or any OpenAI-compatible proxy target. Zero npm dependencies, single `gateway.js` file.

This doesn't duplicate Envoy or rlmgw — different job entirely. Envoy unifies self-hosted GPU backends for every consumer in the platform; rlmgw injects repo context in front of one backend; local-harness lets *one developer* choose, per session, between burning a paid subscription and using the self-hosted fleet. Port 4000 collides with Envoy's default only on paper — Envoy runs once, shared, on the gateway host; local-harness runs per-developer, loopback-only, on their own machine.

**Security, checked against the actual source, not the README:** this went through what looks like an AI-assisted security review (`SECURITY-INTAKE-REPORT.md`/`SECURITY-SUMMARY.md` in the repo). Confirmed fixed in the latest commit: the AppleScript terminal-injection path (`openLoginTerminal` now applies a second `quoted form of` escaping layer) and the XSS risk in how the admin token gets embedded into the dashboard HTML. The "Critical — Command Injection via Admin API" finding is mitigated in practice — `validateConfig()` allow-lists `lane.command` before any change is accepted, and more fundamentally the CLI lanes use `spawn(command, args)` rather than a shell string, so shell metacharacters were never actually interpreted at runtime. **Still open:** the admin token is passed as a `?token=...` URL query parameter (visible in the README's own printed example), which leaks into browser history and request logs — real, if lower-blast-radius given the loopback-only binding.

```bash
cd /Users/laurianlamba/Gitlab/LocalProjects/mitko/mitkox-repos/local-harness
node gateway.js
# or: ./start.sh
```

Open the Admin GUI URL printed in the terminal, and either configure the `local` lane there by hand, or wire it to this platform's Envoy/rlmgw automatically:

```bash
python3 ../deploy-platform.py --phase 7.8
```

which edits only the `local` lane's `target`/`apiKey` in `config.json` — your Claude/Gemini/Copilot lanes are untouched. See [Automated Deployment](#automated-deployment-human-in-the-loop) below for what that script does and doesn't do on its own.

---

## Network Topology & Firewall Rules

### Port Assignment Reference

Every port below is a **default**, not a requirement. To actually change one, edit the real knob listed in "Configured via" — at deploy time, in the phase section it links to — then update every *other* place that connects to it (the table's "Direction" column shows who that is), since each consumer has its own hardcoded reference to the old port until you change it too. (`inventory.yaml` used to carry a duplicate `ports:` list; removed — this table, with the real knob attached, is strictly more useful and there's no reason to keep two copies in sync by hand.)

| Service | Port | Protocol | Hosts | Direction | Configured via |
|---------|------|----------|-------|-----------|----------------|
| Ollama | 11434 | HTTP | Mac, Jetsons | ← Envoy | `OLLAMA_HOST=0.0.0.0:<port>` (systemd override, [1.3](#phase-1--model-serving)) |
| ds4-server | 8080 | HTTP | Mac | ← Envoy | `--port` flag ([1.2](#phase-1--model-serving)) |
| vllm-turboquant | 8000 | HTTP | OpenShift | ← Envoy, rlmgw, sonic | `--port` flag on `vllm serve` ([1.1](#phase-1--model-serving)) |
| **Envoy** | **4000** | **HTTP** | **linux-cpu-01** | **← All clients. Admin :9901 is localhost-only.** | `port_value` in `envoy.yaml`'s listener block **and** the matching `-p` in the systemd unit's `docker run` — both must change together, or the container port mapping breaks ([2.1](#phase-2--gateway)) |
| rlmgw | 8010 | HTTP | linux-cpu-01 | ← coding tools (megacode, SkillOpt, agents) only | `RLMGW_PORT` in `rlmgw.env` ([2.2](#phase-2--gateway)) |
| sonic | 9000 | WS/HTTP | OpenShift | ← Agents, devs | uvicorn's `--port` in the Dockerfile `CMD` ([2.3](#phase-2--gateway)) — also check `config.py`/`.env.example` for a native setting that may need to match |
| background-coding-agents | 8080 | HTTP | OpenShift | ← API clients (code doesn't enforce this — confirm at runtime) | uvicorn's `--port` in the container command ([3.3](#phase-3--agent-execution)) — nothing internal to fight, the app doesn't read a port env var at all |
| fabrica | 8080 | HTTP | OpenShift (Kata sandbox) | ← bg-agents | `-listen` flag if run directly, or Helm's `--set manager.listenPort=<port> --set service.port=<port>` ([3.1](#phase-3--agent-execution)) — both Helm values need to match |
| ain (P2P) | 4001 | TCP/UDP | Everywhere | ↔ Mesh peers (multiaddr, not host:port) | `--p2p-listen /ip4/0.0.0.0/tcp/<port>` on `ain-node` ([6.3](#phase-6--edge-agents)) — changing it means updating every peer's `--bootstrap` multiaddr to match, not just this node |
| ain (API) | 8787 | HTTP | Everywhere | ← Local queries | `--http-listen 0.0.0.0:<port>` on `ain-node` ([6.3](#phase-6--edge-agents)) |
| dede | 5057 | HTTP | Mac | ← Local browser | `--url http://127.0.0.1:<port>` on the `serve` subcommand ([7.2](#phase-7--developer-tools)) |

The one port intentionally **not** meant to be freely reassigned outward-facing is Envoy's admin port (`9901`) — wherever you put it, keep it bound to `127.0.0.1` only. It exposes internal stats and a config dump; it was never meant to be reachable from other hosts.

### Firewall Rules

**linux-cpu-01 (gateway host):**

```bash
sudo ufw default deny incoming
sudo ufw allow ssh
sudo ufw allow 4000/tcp comment "Envoy unified gateway"
sudo ufw allow 8010/tcp comment "rlmgw coding-context proxy"
sudo ufw allow 4001/tcp comment "ain P2P mesh"
sudo ufw allow 8787/tcp comment "ain-node HTTP API"
sudo ufw enable
# 9901 (Envoy admin) deliberately NOT opened — bound to 127.0.0.1 in envoy.yaml.
```

**linux-gpu-XX (training hosts):**

```bash
sudo ufw default deny incoming
sudo ufw allow ssh
sudo ufw allow 4001/tcp comment "ain P2P mesh"
sudo ufw allow 8787/tcp comment "ain-node HTTP API"
sudo ufw enable
```

**Jetson nodes:**

```bash
sudo ufw default deny incoming
sudo ufw allow ssh
sudo ufw allow 11434/tcp comment "Ollama API"
sudo ufw allow 4001/tcp comment "ain P2P mesh"
sudo ufw allow 8787/tcp comment "ain-node HTTP API"
sudo ufw enable
```

---

## Where Configuration Actually Lives

The real config surface for every component in this platform — not `inventory.yaml`, which is a lookup table, but the actual file/flags/env-vars the running software reads. "No file" means it's CLI-flags-only with nothing persisted beyond your shell history or the systemd unit that invoked it.

| Component | Phase | Real config surface | Location |
|---|---|---|---|
| vllm-turboquant | 1.1 | CLI flags to `vllm serve` | No file — flags only |
| ds4-zgx-gb10 | 1.2 | CLI flags to `ds4-server` | Persisted only inside the launchd plist's argument list |
| Ollama | 1.3 | systemd drop-in | `/etc/systemd/system/ollama.service.d/override.conf` |
| Envoy | 2.1 | Static bootstrap config | `/opt/ai-platform/envoy/envoy.yaml` |
| rlmgw | 2.2 | `RLMGW_*` env vars | `/opt/ai-platform/rlmgw/rlmgw.env` |
| sonic | 2.3 | Env vars | Embedded directly in `openshift/sonic-deployment.yaml`'s Deployment spec |
| fabrica | 3.1 | Helm values (becomes CLI flags to the binary at runtime — no config file of its own) | `deploy/helm/fabrica/values-production.yaml` in the fabrica repo, plus `--set` overrides |
| firecracker-agentfs | 3.2 | No runtime config | Build artifacts land at `/opt/ai-platform/firecracker-agentfs/rootfs/` |
| background-coding-agents | 3.3 | Env vars | Embedded directly in `openshift/background-coding-agents.yaml`'s Deployment spec |
| ai-coding-factory | 3.4 | `.env` + opencode config | `.env` and `.opencode/opencode.json` in the ai-coding-factory repo checkout |
| aegis | 4.1 | No config file for the CLI itself; `AEGIS_AI_*` env vars for its AI-review step | Signed plans land at `~/.local/share/aegis/plans/*.json` |
| megacode | 4.2 | `AUDIT_*` env vars | No file — shell export or CI pipeline variables |
| tnt | 4.3 | `.env` | In the tnt repo checkout |
| SDFT | 5.1 | CLI flags to `main.py` | No file |
| sparser-faster-llms | 5.2 | Positional CLI args + fixed presets | `cfgs/run_cfg/*.yaml` in the repo — not user-edited, selected by name |
| oda | 6.1 | Interactive prompts | No file |
| oda-r | 6.2 | `config.yaml` | `/opt/ai-platform/oda-r/config.yaml`, written per Jetson |
| ain-node | 6.3 | CLI flags | No config file; identity/event data lives under `--data-dir` (`/opt/ai-platform/ain-data`, or `~/.ain` on Mac) |
| SkillOpt | 7.1 | `configs/*.yaml` | `configs/local_mitko.yaml` in the SkillOpt repo checkout |
| dede | 7.2 | CLI flags | No file |
| ccar | 7.3 | Claude Code settings | `~/.claude/settings.json`, patched by `install-global.sh` |
| llama3.2-coreml | 7.4 | CLI flags | No file |
| omarchy-ai | 7.5 | Interactive install | No file |
| fteplusai | 7.6 | The markdown files themselves are the config | `agents/*.agent.md`, `skills/*.skill.md` in the fteplusai repo checkout |
| aimatch | 7.7 | CLI flags | No file |

---

## API Examples

### Chat Completion (through Envoy — the unified gateway)

```bash
# Envoy's Lua filter reads "model" from the body and routes accordingly.
# "production" goes through the aggregate cluster's automatic
# vllm-turboquant -> ds4-server -> Ollama failover chain.
curl -s http://10.0.4.10:4000/v1/chat/completions \
  -H "Authorization: Bearer sk-REPLACE-ME" -H "Content-Type: application/json" \
  -d '{
    "model": "edge-jetson",
    "messages": [
      {"role": "system", "content": "You are a helpful assistant."},
      {"role": "user", "content": "Write a Python function to reverse a linked list"}
    ],
    "temperature": 0.3,
    "max_tokens": 500
  }' | jq .choices[0].message.content

curl -s http://10.0.4.10:4000/v1/chat/completions \
  -H "Authorization: Bearer sk-REPLACE-ME" -H "Content-Type: application/json" \
  -d '{
    "model": "production",
    "messages": [{"role": "user", "content": "Explain microservices architecture"}],
    "max_tokens": 1000
  }' | jq .
```

### Coding query with repo context (through rlmgw)

```bash
# Scoped to whatever RLMGW_REPO_ROOT it was started with. Rejects streaming
# — send "stream": false or omit it.
curl -s http://10.0.4.10:8010/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [{"role": "user", "content": "Where is the retry logic for the upstream client defined?"}]
  }' | jq .
```

### Queue a Migration Job (background-coding-agents)

```bash
curl -s -X POST \
  http://background-coding-agents-ai-agents.apps.ocp.ai-platform.internal/api/v1/migrations/run \
  -H "Content-Type: application/json" \
  -d '{
    "migration_name": "<name of a migration YAML under fleet_manager/migrations/>",
    "dry_run": true,
    "target_sites": ["<site-name>"],
    "provider": "openai_compat",
    "model": "<your-model-name>"
  }' | jq .

curl -s http://background-coding-agents-ai-agents.apps.ocp.ai-platform.internal/api/v1/migrations/jobs/<job-id> | jq .
```

### Security Scan with megacode

```bash
cd ~/projects/my-dotnet-app
security-audit --source-root . --output-report ./security-report.md

ssh admin@10.0.4.10 "
  cd /opt/ai-platform/megacode &&
  source venv/bin/activate &&
  security-audit --source-root /path/to/project --output-report /tmp/report.md
"
scp admin@10.0.4.10:/tmp/report.md ./security-report.md
```

### Start a Distillation Job (SDFT)

```bash
ssh admin@10.0.3.10 "
  cd /opt/ai-platform/SDFT &&
  conda activate ai-platform &&
  nohup python3 main.py \
    --output_dir /opt/ai-platform/models/qwen3-0.6b-distilled \
    --model_name_or_path Qwen/Qwen3-0.6B \
    --vllm_server_base_url http://vllm-turboquant-ai-serving.apps.ocp.ai-platform.internal/v1 \
    > /opt/ai-platform/logs/sdft-$(date +%Y%m%d).log 2>&1 &
  echo \"SDFT job started. PID: \$!\"
"
ssh admin@10.0.3.10 "tail -f /opt/ai-platform/logs/sdft-*.log"
```

### Check Agent Mesh Status (ain)

```bash
curl -s http://localhost:8787/v1/node/info | jq '.p2p'

curl -s -X POST http://localhost:8787/v1/events \
  -H "Content-Type: application/json" -d @signed-event.json
```

---

## Monitoring & Troubleshooting

### Quick Health Check — All Services

```bash
#!/usr/bin/env bash
# health-check.sh — Run from any machine on the network
set -euo pipefail

echo "=== Envoy (unified gateway) ==="
# No unauthenticated /health path on :4000 — a bare POST with no token
# should get 401 (proves it's up and enforcing auth); connection failure
# means it's actually down.
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST http://10.0.4.10:4000/v1/chat/completions -d '{}')
[ "$CODE" = "401" ] && echo " ✓ (reachable, auth enforced)" || echo " ✗ DOWN or misconfigured (HTTP ${CODE:-none})"

echo "=== rlmgw (coding-context proxy) ==="
curl -sf http://10.0.4.10:8010/healthz && echo " ✓" || echo " ✗ DOWN"

echo "=== vllm-turboquant (OpenShift) ==="
curl -sf http://vllm-turboquant-ai-serving.apps.ocp.ai-platform.internal/health && echo " ✓" || echo " ✗ DOWN"

echo "=== sonic (OpenShift) ==="
curl -sf http://sonic-ai-gateways.apps.ocp.ai-platform.internal/healthz && echo " ✓" || echo " ✗ DOWN"

echo "=== background-coding-agents (OpenShift) ==="
curl -sf http://background-coding-agents-ai-agents.apps.ocp.ai-platform.internal/api/v1/health && echo " ✓" || echo " ✗ DOWN"

echo "=== ds4-server (Mac) ==="
curl -sf http://10.0.1.10:8080/v1/models && echo " ✓" || echo " ✗ DOWN"

echo "=== Ollama (Jetsons) ==="
for IP in 10.0.2.10 10.0.2.11 10.0.2.12 10.0.2.13 10.0.2.14; do
  printf "  jetson@${IP}: "
  curl -sf http://${IP}:11434/api/tags > /dev/null && echo "✓" || echo "✗"
done

echo "=== ain mesh ==="
NODE_INFO=$(curl -sf http://localhost:8787/v1/node/info 2>/dev/null)
echo "  Node info: ${NODE_INFO:-DOWN}"

echo "=== fabrica (Sandbox, OpenShift) ==="
curl -sf http://fabrica-ai-agents.apps.ocp.ai-platform.internal/health && echo " ✓" || echo " ✗ DOWN"
```

### Service Logs by Host Type

**Linux hosts (systemd):**

```bash
ssh admin@10.0.4.10 "journalctl -u envoy-gateway -f --no-pager -n 50"
ssh admin@10.0.4.10 "journalctl -u rlmgw -f --no-pager -n 50"
ssh admin@10.0.4.10 "journalctl -u ain-node -f --no-pager -n 50"
```

**Mac (launchd):**

```bash
tail -f /tmp/ds4-server.log
tail -f /tmp/ain-node.log
launchctl list | grep ai-platform
```

**OpenShift:**

```bash
oc logs -f -l app=vllm-turboquant -n ai-serving --tail=100
oc logs -f -l app=sonic -n ai-gateways --tail=100
oc logs -f -l app=background-coding-agents -n ai-agents --tail=100
oc logs -f -l app=fabrica -n ai-agents --tail=100
oc get pods -n ai-serving -n ai-gateways -n ai-agents
```

**Jetsons:**

```bash
ssh nvidia@10.0.2.10 "journalctl -u ollama -f --no-pager -n 50"
ssh nvidia@10.0.2.10 "journalctl -u ain-node -f --no-pager -n 50"
```

### GPU Monitoring

```bash
sudo asitop                                      # Mac (Apple Silicon)
ssh admin@10.0.3.10 "watch -n 2 nvidia-smi"       # Linux GPU — NVIDIA
ssh admin@<amd-host-ip> "watch -n 2 rocm-smi"     # Linux GPU — AMD
ssh nvidia@10.0.2.10 "jtop"                       # Jetsons (always NVIDIA/Tegra)

oc exec -it $(oc get pods -l app=vllm-turboquant -n ai-serving -o jsonpath='{.items[0].metadata.name}') \
  -n ai-serving -- nvidia-smi
```

### Common Issues

| Problem | Check | Fix |
|---------|-------|-----|
| rlmgw can't reach Ollama on Jetson | `curl http://10.0.2.10:11434/api/tags` | Set `OLLAMA_HOST=0.0.0.0` on Jetson, restart Ollama |
| vLLM OOM on OpenShift | `oc logs -l app=vllm-turboquant` | Reduce `--max-model-len` or increase GPU memory limit |
| ain mesh shows no peers | `curl localhost:8787/v1/node/info` | Check firewall allows 4001 TCP/UDP and 8787 TCP; verify `--bootstrap` multiaddrs include the real `/p2p/<peer-id>` suffix |
| ds4-server won't start on Mac | `cat /tmp/ds4-server.err` | Likely needs model download first: `./download_model.sh` |
| SDFT training crashes | `tail -f /opt/ai-platform/logs/sdft-*.log` | Check CUDA memory; adjust the batch-size flag directly (`python3 main.py --help`). Adapted for CUDA 12.x on L4/L40S, not the GB10 hardware its own README documents |
| aegis blocks a package install | `aegis policy` | `aegis review` the plan, then `aegisctl sign`/`aegisctl apply` — no `aegis approve` shortcut |
| vllm-turboquant container won't see AMD GPUs | `docker exec <container> rocm-smi` | Confirm `--device=/dev/kfd --device=/dev/dri --group-add video` were passed; check `PYTORCH_ROCM_ARCH` matches `rocminfo \| grep gfx` |
| TurboQuant flags rejected or silently ignored | `oc logs -l app=vllm-turboquant \| grep -i turboquant` | Expected on L4/L40S and all AMD GPUs — only activates on RTX A6000/SM86 or GB10/SM121 |
| sparser-faster-llms won't build/run TwELL kernels | `python scripts/check_gb10_cuda.py --build-twell` | Likely the Hopper-vs-Ada mismatch (Phase 5.2), not a config issue |
| Gaudi2 host sits idle | — | Expected — no repo here has a Habana/SynapseAI path |

---

## Component Reference

Every component in this workspace, one entry each: what it is, what it actually needs, and the minimal command to run it completely on its own — no other repo, no `inventory.yaml` entry, nothing else from this platform. Full deployment (systemd units, OpenShift manifests, health checks) is in the phase section linked from each entry; this is the fast path if you're starting from one component instead of the whole platform.

**The pattern that makes this possible:** across every phase audit behind this guide, not one of the 24 mitkox repos was found to have a hard, code-level dependency on another repo in this workspace. Every apparent cross-repo integration an earlier draft of this guide assumed — fabrica reading firecracker-agentfs's rootfs, background-coding-agents calling fabrica's sandbox API — turned out to be fabricated. What each repo actually needs is either generic infrastructure (a GPU, Kubernetes+Kata, Claude Code) or a single swappable `--base-url`/`api_base`/`*_ENDPOINT` config field pointed at any OpenAI-compatible server — this platform's Envoy, a bare `vllm serve` process, Ollama, OpenAI itself, or nothing.

### Model Serving

**vllm-turboquant** — vLLM fork with quantized-KV-cache serving. *Needs:* a GPU (CUDA or ROCm) + Docker. *Standalone:*
```bash
docker run -d --name vllm-turboquant --device=/dev/kfd --device=/dev/dri \
  --group-add video --ipc=host --shm-size 16g -p 8000:8000 \
  vllm-turboquant:rocm vllm serve <model> --tensor-parallel-size 2 --host 0.0.0.0 --port 8000
```
No rlmgw, no Envoy, no `inventory.yaml` entry required — `curl http://<host>:8000/v1/chat/completions` works immediately. Full deploy: [Phase 1.1](#phase-1--model-serving).

**ds4-zgx-gb10** — native Metal inference for DeepSeek-V4/GLM-5.2. *Needs:* Apple Silicon Mac with **~96GB+ RAM** — confirmed the smallest downloadable model is 81GB on disk; there is no smaller supported quant, so a typical 16–32GB laptop cannot run this at all, regardless of how it's deployed. *Standalone (only if your RAM clears that bar):* `make && ./download_model.sh q4-imatrix && ./ds4-server -m gguf/<file>.gguf --ctx 100000 --kv-disk-dir /tmp/ds4-kv --kv-disk-space-mb 8192 --host 0.0.0.0`. Full deploy: [Phase 1.2](#phase-1--model-serving).

### Gateway

**rlmgw** — repo-context-injecting proxy in front of one backend. *Needs:* one OpenAI-compatible endpoint (any). *Standalone:* `pip install -e '.[gw]'` then set `RLMGW_UPSTREAM_BASE_URL`/`RLMGW_REPO_ROOT` and run `python -m rlmgw.server`. Full deploy: [Phase 2.2](#phase-2--gateway).

**sonic** — WebSocket agent gateway. *Needs:* one vLLM-compatible endpoint (`VLLM_URL`). *Standalone:* build the Dockerfile shown in [Phase 2.3](#phase-2--gateway) and `docker run -p 9000:9000 -e VLLM_URL=<any vllm> sonic:latest`.

**Envoy** *(added, not a repo)* — unifying gateway. *Needs:* whatever backends you point it at — works with just one. Config and validated live test: [Phase 2.1](#phase-2--gateway).

### Agent Execution

**fabrica** — Kata-Container microVM sandboxes. *Needs:* Kubernetes + Kata Containers + Cloud Hypervisor. *Standalone:* `helm upgrade --install fabrica deploy/helm/fabrica -f values-dev.yaml` against any Kata-enabled cluster — doesn't need OpenShift specifically. Full deploy: [Phase 3.1](#phase-3--agent-execution).

**firecracker-agentfs** — Firecracker microVM boot artifacts. *Needs:* KVM + Firecracker + NFS. *Standalone:* `./build-kernel.sh && ./build-rootfs.sh` on any KVM-capable Linux host — independent of fabrica. Full deploy: [Phase 3.2](#phase-3--agent-execution).

**background-coding-agents** — PLC/SCADA migration fleet manager. *Needs:* one OpenAI-compatible endpoint (`LLM_BASE_URL`). *Standalone:* `pip install -e '.[dev]' && uvicorn background_coding_agents.api.app:app --port 8080` with `LLM_BASE_URL` pointed anywhere. Full deploy: [Phase 3.3](#phase-3--agent-execution).

**ai-coding-factory** — `opencode`-driven scaffolding framework. *Needs:* the `opencode` CLI + one endpoint. *Standalone:* set `.env` (`OPENCODE_BASE_URL`/`OPENCODE_API_KEY`/`OPENCODE_MODEL`) and run `opencode run "..."` — no build/deploy step exists. Full detail: [Phase 3.4](#phase-3--agent-execution).

### Security

**aegis** — signed package-install broker. *Needs:* nothing but the binaries; its AI-review step calls a configurable endpoint. *Standalone:* `cargo build --release && sudo cp target/release/{aegis,aegisctl,aegisd,aegis-reviewd} /usr/local/bin/` then `aegis npm install <pkg> --plan`. Full deploy incl. Linux daemon pipeline: [Phase 4.1](#phase-4--security-pipeline).

**megacode** — RLM-based .NET security auditor. *Needs:* one endpoint (`AUDIT_LM_API_BASE`). *Standalone:* `pip install -e '.[dev]' && security-audit --source-root . --output-report report.md`. Full deploy: [Phase 4.2](#phase-4--security-pipeline).

**tnt** — Roadrunner/Coyote vulnerability triage. *Needs:* two independently configurable endpoints. *Standalone:* `uv sync` (python/) + `npm install` (node/), then `./scripts/verify-harness.sh --repo-root <path>`. Full deploy: [Phase 4.3](#phase-4--security-pipeline).

### Model Optimization

**SDFT** — self-distillation training loop. *Needs:* a CUDA GPU + one external vLLM teacher endpoint (any vLLM, not necessarily this platform's). *Standalone:* `python3 main.py --output_dir <dir> --model_name_or_path Qwen/Qwen3-0.6B --vllm_server_base_url <any vllm>`. Full deploy: [Phase 5.1](#phase-5--model-optimization).

**sparser-faster-llms** — sparse-transformer training with custom CUDA kernels. *Needs:* a CUDA GPU (H100-class kernels) — no network dependency at all. *Standalone:* `bash scripts/install.sh --full && ./launch.sh <n-gpus> sparsity_gated_1p5b zero1`. Full deploy: [Phase 5.2](#phase-5--model-optimization).

**Thinking-with-Visual-Primitives** — no code ships yet; nothing to run.

### Edge Agents

**oda** — one-command Linux AI dev environment setup. *Needs:* a Linux host, interactive session (see [Phase 6.1](#phase-6--edge-agents) for why). *Standalone:* `./oda.sh`, run interactively.

**oda-r** — DSPy-branded (but not DSPy-based) reasoning compiler. *Needs:* one completion-style endpoint (`server_url`). *Standalone:* `pip install -r requirements.txt && python odar.py <file> --config config.yaml`. Full deploy: [Phase 6.2](#phase-6--edge-agents).

**ain** — P2P signed-event mesh. *Needs:* nothing — no LLM dependency of any kind. *Standalone:* `cargo build --release && ./target/release/ain-node --http-listen 0.0.0.0:8787 --p2p-listen /ip4/0.0.0.0/tcp/4001 --data-dir ./data` — runs as a single isolated node with no `--bootstrap` peers if you just want to try it. Full deploy incl. the local-signing model: [Phase 6.3](#phase-6--edge-agents).

### Developer Tools

**SkillOpt** — skill-document/prompt optimizer. *Needs:* one local OpenAI-compatible server. *Standalone:* `pip install -e . && python3 scripts/train.py --config <cfg>`. Full deploy: [Phase 7.1](#phase-7--developer-tools).

**dede** — .NET dependency/blast-radius explorer. *Needs:* .NET SDK only — no network dependency. *Standalone:* `dotnet run --project src/DogEatDog.DependencyExplorer.Cli -- scan <path> -o graph.json` then `... -- serve graph.json`. Full deploy: [Phase 7.2](#phase-7--developer-tools).

**ccar** — Claude-Code-native autonomous benchmark loop. *Needs:* Claude Code itself. *Standalone:* `./install-global.sh`, then `/autoresearch <goal>` inside a Claude Code session. Full deploy: [Phase 7.3](#phase-7--developer-tools).

**llama3.2-coreml** — Llama 3.2 → Core ML export/quantization. *Needs:* Mac + Python — no network dependency. *Standalone:* `python3 run_model.py --output-dir ./export` (export only; no inference path ships in this repo). Full deploy: [Phase 7.4](#phase-7--developer-tools).

**omarchy-ai** — one-time Arch+Hyprland workstation installer. *Needs:* a fresh Arch Linux install. *Standalone:* `curl -fsSL .../boot.sh | bash`, run interactively, locally. Full deploy: [Phase 7.5](#phase-7--developer-tools).

**fteplusai** — vendor-replacement program agents/skills. *Needs:* an AI-agent runtime to load the markdown into (Claude Code, etc.) — nothing to install. Full detail: [Phase 7.6](#phase-7--developer-tools).

**aimatch** — Extract→Search→Reason→Calibrate profile matcher. *Needs:* nothing by default (TF-IDF heuristic); optionally one endpoint via `--use-local-llm`. *Standalone:* `pip install -e . && aimatch run --queries q.jsonl --candidates c.jsonl --output out.jsonl`. Full deploy: [Phase 7.7](#phase-7--developer-tools).

**local-harness** — not a mitkox repo (`github.com/aidotse/local-harness`). Personal, loopback-only lane switcher between Claude/Gemini subscriptions and any OpenAI-compatible endpoint. *Needs:* Node.js 18+, nothing else (zero npm dependencies). *Standalone:* `node gateway.js`, then configure lanes from the Admin GUI, or run `python3 deploy-platform.py --phase 7.8` to point its `local` lane at this platform's own Envoy/rlmgw. Full deploy: [Phase 7.8](#phase-7--developer-tools).

### Not part of this platform

**aks-edge-utils** — a fork of Microsoft's official AKS Edge Essentials repo. Present in the workspace but not referenced anywhere in `inventory.yaml` or this guide — unrelated to everything above.
