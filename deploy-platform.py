#!/usr/bin/env python3
"""
deploy-platform.py — human-in-the-loop deployment orchestrator for the AI
Engineering Platform, built strictly from the verified commands in
DEPLOYMENT_GUIDE.md. This script does not replace that document — it is a
convenience layer on top of it. If the two ever disagree, the guide's phase
sections are the source of truth; file a note and fix this script to match.

WHAT "AUTOMATED" MEANS HERE, HONESTLY:

  Phases 1, 2, 4, 6, and 7.8 are genuinely automated: this script runs the
  real build/deploy commands over SSH, with a confirmation prompt before
  anything that builds an image, restarts a shared service, or touches a
  host whose inventory.yaml entry still has a placeholder value.

  Phases 3 and 5, and the interactive installers in Phase 6.1/6.7, are
  GUIDED CHECKLIST mode only: the script prints each real command from the
  guide and asks you to confirm before/after running it, but does not
  execute Kata-operator cluster setup or kick off a training job on its
  own. Those are cluster-wide or long-running/resource-consuming decisions
  that deserve an explicit human action, not a script silently deciding for
  you. This is a deliberate scope boundary, not a gap to "fix" later.

Usage:
  python3 deploy-platform.py                  # interactive menu
  python3 deploy-platform.py --phase 1         # run one phase's automated steps
  python3 deploy-platform.py --phase 2.1       # run one component
  python3 deploy-platform.py --list            # show what's automated vs. guided
  python3 deploy-platform.py --yes             # DANGEROUS: skip confirmations
                                                # (see the warning where it's used)

Requires: Python 3.9+, PyYAML (`pip3 install pyyaml`), and working SSH key
access to every host you target (see DEPLOYMENT_GUIDE.md's Prerequisites
section — this script does not set that up for you, on purpose: it's the
one prerequisite that has to exist before anything here can run at all).
"""
from __future__ import annotations

import argparse
import json
import pathlib
import platform
import shlex
import subprocess
import sys

try:
    import yaml
except ImportError:
    print("error: this script needs PyYAML -- run: pip3 install pyyaml", file=sys.stderr)
    sys.exit(1)

REPO_ROOT = pathlib.Path(__file__).parent.resolve()
INVENTORY_PATH = REPO_ROOT / "inventory.yaml"

PLACEHOLDER_MARKERS = {"TBD", "0", "", None}


class AbortDeployment(Exception):
    """Raised to stop the current phase cleanly -- caught in main(), not a crash."""


class Ctx:
    """Carries the --yes flag and inventory data through a run without globals."""

    def __init__(self, auto_yes: bool, inventory: dict):
        self.auto_yes = auto_yes
        self.inv = inventory
        self.local_mac: dict | None = None  # set by resolve_local_mac() in main()


# ---------------------------------------------------------------------------
# Human-in-the-loop primitives -- every phase function below is built on
# top of these three. Nothing in this script executes a remote command
# without going through confirm() first, except read-only checks (SSH
# connectivity tests, `--version` calls).
# ---------------------------------------------------------------------------

def confirm(ctx: Ctx, prompt: str, default: bool = False) -> bool:
    if ctx.auto_yes:
        print(f"\n>>> {prompt} [--yes: auto-confirmed]")
        return True
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        ans = input(f"\n>>> {prompt} {suffix} ").strip().lower()
        if not ans:
            return default
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        print("please answer y or n")


def ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    ans = input(f">>> {prompt}{suffix}: ").strip()
    return ans or (default or "")


def section(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def run_local(cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    print(f"$ {cmd}")
    return subprocess.run(cmd, shell=True, check=check)


def ssh_identity_args(ctx: Ctx) -> list[str]:
    """['-i', '<path>'] from global.ssh_key, expanded -- or [] to fall back
    to ssh's own default identity resolution if the field is unset. Exists
    so inventory.yaml's `global.ssh_key` (documented in DEPLOYMENT_GUIDE.md's
    Prerequisites as *the* key that must be authorized on every host)
    actually reaches every ssh/scp/rsync call, instead of being a purely
    descriptive field nothing reads."""
    key = ctx.inv.get("global", {}).get("ssh_key")
    if not key:
        return []
    return ["-i", str(pathlib.Path(key).expanduser())]


def rsync_ssh_flag(ctx: Ctx) -> str:
    """Shell-ready ' -e ssh -i <path>' fragment to splice into an inline
    rsync command string -- '' if no global.ssh_key is set."""
    args = ssh_identity_args(ctx)
    return f" -e {shlex.quote('ssh ' + ' '.join(args))}" if args else ""


def scp_identity_prefix(ctx: Ctx) -> str:
    """Shell-ready '-i <path> ' prefix for an inline scp command string --
    '' if no global.ssh_key is set."""
    args = ssh_identity_args(ctx)
    return (" ".join(shlex.quote(a) for a in args) + " ") if args else ""


def run_remote(ctx: Ctx, user: str, host: str, remote_cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    cmd = ["ssh", *ssh_identity_args(ctx), f"{user}@{host}", remote_cmd]
    print("$ " + " ".join(shlex.quote(c) for c in cmd[:-1]) + " " + shlex.quote(remote_cmd))
    return subprocess.run(cmd, check=check)


def ssh_reachable(ctx: Ctx, user: str, host: str) -> bool:
    r = subprocess.run(
        ["ssh", *ssh_identity_args(ctx), "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", f"{user}@{host}", "echo ok"],
        capture_output=True, text=True,
    )
    return r.returncode == 0 and r.stdout.strip() == "ok"


def require_real_value(value, field_name: str, owner: str) -> None:
    """Refuse to proceed on a placeholder inventory.yaml value -- this is the
    main safety net against silently deploying to '10.0.5.10' or 'TBD'."""
    sval = str(value)
    if value in PLACEHOLDER_MARKERS or "REPLACE" in sval.upper() or sval == "TBD":
        raise AbortDeployment(
            f"{owner}'s {field_name} is still a placeholder ({value!r}) in inventory.yaml. "
            f"Fill in the real value before deploying to this host."
        )


def require_ssh(ctx: Ctx, user: str, host: str, owner: str) -> None:
    if not ssh_reachable(ctx, user, host):
        raise AbortDeployment(
            f"Can't reach {user}@{host} ({owner}) over SSH. Confirm the host is up, "
            f"the IP in inventory.yaml is real, and your key is authorized "
            f"(see DEPLOYMENT_GUIDE.md Prerequisites -> ssh-copy-id)."
        )


# ---------------------------------------------------------------------------
# Inventory helpers
# ---------------------------------------------------------------------------

def load_inventory() -> dict:
    if not INVENTORY_PATH.exists():
        print(f"error: {INVENTORY_PATH} not found", file=sys.stderr)
        sys.exit(1)
    return yaml.safe_load(INVENTORY_PATH.read_text())


def detect_local_mac_hostname() -> str | None:
    """This machine's real Bonjour/mDNS name (System Settings -> Sharing ->
    Local hostname), via `scutil`. This is NOT the same thing as a
    mac_workstations entry's `hostname` field in inventory.yaml, which is
    just a human-chosen label -- the two only match if the Mac was actually
    renamed to it. Since Envoy's ds4-zgx-gb10 backend addressing
    (_build_envoy_backends) depends on the *real* name for `<name>.local` to
    resolve at all, this is the only correct source for it. macOS only;
    None elsewhere or if scutil fails."""
    if platform.system() != "Darwin":
        return None
    try:
        r = subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True, text=True, timeout=3)
        name = r.stdout.strip()
        return name if r.returncode == 0 and name else None
    except (OSError, subprocess.SubprocessError):
        return None


def detect_remote_mac_hostname(ctx: Ctx, user: str, ip: str) -> str | None:
    """Same as detect_local_mac_hostname, but for a Mac reached over SSH --
    used to verify a ds4-zgx-gb10 backend that isn't the machine this
    script happens to be running on. Best-effort: None on any failure,
    callers fall back to inventory.yaml's label."""
    try:
        r = subprocess.run(
            ["ssh", *ssh_identity_args(ctx), "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
             f"{user}@{ip}", "scutil --get LocalHostName"],
            capture_output=True, text=True, timeout=10,
        )
        name = r.stdout.strip()
        return name if r.returncode == 0 and name else None
    except (OSError, subprocess.SubprocessError):
        return None


def _write_inventory_hostname(old: str, new: str) -> bool:
    """Rewrites just the one 'hostname: <old>' line to <new> via exact text
    substitution -- deliberately NOT yaml.dump(ctx.inv), which would
    reformat every list in the file (inline `[a, b]` -> block `- a`) as a
    side effect of fixing a single field. Returns False (changes nothing)
    if `old` isn't uniquely findable as text, so callers can fall back to
    telling the user to edit it by hand."""
    text = INVENTORY_PATH.read_text()
    old_line = f"hostname: {old}"
    if text.count(old_line) != 1:
        return False
    INVENTORY_PATH.write_text(text.replace(old_line, f"hostname: {new}", 1))
    return True


def resolve_local_mac(ctx: Ctx) -> dict | None:
    """Which mac_workstations entry (if any) IS this machine -- auto-detected
    from the OS, never typed in. Also reconciles inventory.yaml's `hostname`
    label against reality: a stale label here silently breaks Envoy's
    `<hostname>.local` backend address (_build_envoy_backends), so it's
    worth fixing at the source. Returns None off macOS, with no
    mac_workstations entries, or if detection/matching can't be resolved."""
    macs = ctx.inv.get("mac_workstations", [])
    if platform.system() != "Darwin" or not macs:
        return None

    real = detect_local_mac_hostname()
    if real is None:
        print("Note: couldn't auto-detect this Mac's real hostname (scutil failed) -- "
              "falling back to inventory.yaml as-is wherever a Mac's identity matters.")
        return None

    match = next((m for m in macs if m.get("hostname") == real), None)
    if match is not None:
        return match

    if len(macs) == 1:
        entry = macs[0]
    else:
        print(f"\nThis machine's real hostname is {real!r} (via scutil) -- doesn't match any "
              f"mac_workstations entry in inventory.yaml by name:")
        for i, m in enumerate(macs):
            print(f"  {i}: {m.get('hostname')} ({m.get('ip', '?')})")
        choice = ask("Which entry is this machine? (blank to skip)", "")
        if not choice:
            return None
        try:
            entry = macs[int(choice)]
        except (ValueError, IndexError):
            print("Not a valid choice -- skipping local-machine detection for this run.")
            return None

    print(f"\ninventory.yaml labels this entry hostname: {entry.get('hostname')!r}, but this "
          f"machine's real (Bonjour/mDNS) hostname is {real!r}. Left mismatched, this silently "
          f"breaks '<hostname>.local' addressing for anything that needs to reach it (e.g. Envoy's "
          f"ds4-zgx-gb10 backend).")
    if confirm(ctx, f"Fix inventory.yaml: hostname -> {real!r}?", default=True):
        old = entry["hostname"]
        if _write_inventory_hostname(old, real):
            entry["hostname"] = real
            print("inventory.yaml updated.")
        else:
            print(f"Couldn't safely auto-edit inventory.yaml (hostname: {old} wasn't uniquely "
                  f"findable as text) -- change it to {real!r} by hand.")
    return entry


def linux_cpu_gateway_host(ctx: Ctx) -> dict:
    hosts = ctx.inv.get("linux_cpu_hosts", [])
    for h in hosts:
        if h.get("role") == "gateway_and_sandbox":
            return h
    if hosts:
        return hosts[0]
    raise AbortDeployment("No linux_cpu_hosts entry in inventory.yaml -- add one first.")


def hosts_running(ctx: Ctx, service: str) -> list[dict]:
    """Every host, of any type, whose services list includes `service`."""
    found = []
    for m in ctx.inv.get("mac_workstations", []):
        if service in m.get("services", []):
            found.append({**m, "_ssh_user": m.get("ssh_user", "dev"), "_kind": "mac"})
    for n in ctx.inv.get("jetson_nodes", {}).get("nodes", []):
        if service in n.get("services", []):
            found.append({**n, "_ssh_user": ctx.inv["jetson_nodes"].get("ssh_user", "nvidia"), "_kind": "jetson"})
    for h in ctx.inv.get("linux_gpu_hosts", []):
        if service in h.get("services", []):
            found.append({**h, "_ssh_user": h.get("ssh_user", "admin"), "_kind": "linux_gpu"})
    for h in ctx.inv.get("linux_cpu_hosts", []):
        if service in h.get("services", []):
            found.append({**h, "_ssh_user": h.get("ssh_user", "admin"), "_kind": "linux_cpu"})
    return found


# ---------------------------------------------------------------------------
# Phase 1 — Model Serving
# ---------------------------------------------------------------------------

NATIVE_VLLM_IMAGES = {
    # Both confirmed to exist on Docker Hub. No entry for "intel"
    # (Gaudi/Habana): no pre-built image exists for it anywhere; that needs
    # its own from-source build (HabanaAI/vllm-fork or upstream vLLM's
    # requirements/hpu.txt), not something this script automates.
    "nvidia": "vllm/vllm-openai:latest",
    "amd": "rocm/vllm:rocm7.14.0_rdna_ubuntu24.04_py3.14_pytorch_2.11.0_vllm_0.23.0",
}


def phase1_model_serving(ctx: Ctx) -> None:
    section("Phase 1.1 — Model Serving (native vLLM)")
    targets = hosts_running(ctx, "vllm")
    if not targets:
        print("No host in inventory.yaml lists vllm as a service. Skipping.")
        return
    for host in targets:
        name, ip, user = host["hostname"], host["ip"], host["_ssh_user"]
        vendor = host.get("gpu_vendor", "nvidia")
        require_real_value(ip, "ip", name)
        require_real_value(vendor, "gpu_vendor", name)
        print(f"\n-- {name} ({ip}), gpu_vendor={vendor} --")
        require_ssh(ctx, user, ip, name)

        if vendor not in NATIVE_VLLM_IMAGES:
            print(f"gpu_vendor={vendor!r} has no known native-vLLM image here -- Habana/Gaudi "
                  f"needs a separate from-source build (HabanaAI/vllm-fork or requirements/hpu.txt), "
                  f"not a Docker Hub pull. See DEPLOYMENT_GUIDE.md Phase 1.1. Skipping {name}.")
            continue

        model = ask("Model to serve (any HF repo -- check 'Which quantization format actually works "
                    "on your GPU' in the guide before picking an FP8-tagged one on hardware that "
                    "can't use it)", "<your-model-repo-or-path>")
        tp_size = ask("--tensor-parallel-size", str(host.get("gpu_count") or 1))
        image = ask(f"vLLM image for {vendor}", NATIVE_VLLM_IMAGES[vendor])
        run_args = ("--device=/dev/kfd --device=/dev/dri --group-add video --ipc=host --shm-size 16g"
                    if vendor == "amd" else "--gpus all")

        if not confirm(ctx, f"Pull {image} on {name} and run it there on :8000?"):
            print(f"Skipped {name}.")
            continue

        run_remote(ctx, user, ip, f"docker pull {image}")
        run_remote(ctx, user, ip, "docker rm -f vllm 2>/dev/null || true", check=False)
        run_remote(
            ctx, user, ip,
            # No "vllm serve" here -- confirmed the AMD RDNA image already
            # bakes in ENTRYPOINT ["vllm", "serve"]; not independently
            # verified for every possible image someone might type at the
            # prompt above, so check its actual entrypoint if this fails
            # with "unrecognized arguments: serve <model>".
            f"docker run -d --name vllm {run_args} -p 8000:8000 {image} "
            f"{shlex.quote(model)} --tensor-parallel-size {tp_size} --host 0.0.0.0 --port 8000",
        )
        print(f"vllm started on {name}:8000 via {image} -- if it fails with "
              f"'unrecognized arguments: serve <model>', check that image's actual entrypoint with "
              f"`docker inspect --format='{{{{.Config.Entrypoint}}}}' {image}` and adjust. "
              f"Health check: curl -s http://{ip}:8000/health")


def phase1_ds4(ctx: Ctx) -> None:
    section("Phase 1.2 — ds4-zgx-gb10 (DwarfStar)")
    for host in ctx.inv.get("mac_workstations", []):
        if "ds4-zgx-gb10" not in host.get("services", []):
            continue
        name, ip, ram = host["hostname"], host["ip"], host.get("ram_gb", 0)
        if ram < 96:
            print(
                f"\n{name} has ram_gb={ram} in inventory.yaml. Confirmed elsewhere in this guide: the "
                f"smallest model ds4-zgx-gb10 ships (DeepSeek V4 Flash q2-imatrix) is 81GB on disk, "
                f'"recommended for 96 and 128GB RAM machines." This will very likely download a model '
                f"it can't load."
            )
            if not confirm(ctx, f"Deploy ds4-zgx-gb10 to {name} anyway?"):
                print(f"Skipped {name} (this is the recommended choice).")
                continue
        require_ssh(ctx, host.get("ssh_user", "dev"), ip, name)
        variant = ask("download_model.sh variant (run --help on the host first if unsure)", "q4-imatrix")
        if not confirm(ctx, f"Build ds4-zgx-gb10 on {name} and download the {variant} model?"):
            continue
        run_local(f"rsync -avz --delete{rsync_ssh_flag(ctx)} --exclude '.git' "
                  f"'{ctx.inv['global']['local_repo_root']}/ds4-zgx-gb10/' '{host.get('ssh_user','dev')}@{ip}:{ctx.inv['global']['remote_base_path']}/ds4-zgx-gb10/'")
        run_remote(ctx, host.get("ssh_user", "dev"), ip,
                   f"cd {ctx.inv['global']['remote_base_path']}/ds4-zgx-gb10 && make && ./download_model.sh {variant}")
        print(f"Built on {name}. Launchd plist + auto-start is still a manual step -- see Phase 1.2 in the guide "
              f"(it needs the exact downloaded GGUF filename, which varies by variant).")


def phase1_ollama(ctx: Ctx) -> None:
    section("Phase 1.3 — Ollama binding (Jetsons)")
    jn = ctx.inv.get("jetson_nodes", {})
    for node in jn.get("nodes", []):
        if "ollama" not in node.get("services", []):
            continue
        name, ip, user = node["hostname"], node["ip"], jn.get("ssh_user", "nvidia")
        require_real_value(ip, "ip", name)
        require_ssh(ctx, user, ip, name)
        if not confirm(ctx, f"Bind Ollama to 0.0.0.0 on {name} and restart it?"):
            continue
        run_remote(ctx, user, ip,
                   "sudo mkdir -p /etc/systemd/system/ollama.service.d && "
                   "printf '[Service]\\nEnvironment=\"OLLAMA_HOST=0.0.0.0\"\\n' | "
                   "sudo tee /etc/systemd/system/ollama.service.d/override.conf >/dev/null && "
                   "sudo systemctl daemon-reload && sudo systemctl restart ollama")
        for model in jn.get("default_models", []):
            run_remote(ctx, user, ip, f"ollama pull {model}", check=False)
        print(f"{name}: check GPU is actually used -- `ssh {user}@{ip} \"ollama run llama3.2:3b 'hi' --verbose 2>&1 | tail -5; ollama ps\"` "
              f"(this script won't parse that output for you; the guide flags real Tegra-detection risk here).")


# ---------------------------------------------------------------------------
# Phase 2 — Gateway
# ---------------------------------------------------------------------------

ENVOY_TEMPLATE = r'''admin:
  address:
    socket_address: {{ address: 127.0.0.1, port_value: 9901 }}

static_resources:
  listeners:
    - name: llm_listener
      address:
        socket_address: {{ address: 0.0.0.0, port_value: 4000 }}
      filter_chains:
        - filters:
            - name: envoy.filters.network.http_connection_manager
              typed_config:
                "@type": type.googleapis.com/envoy.extensions.filters.network.http_connection_manager.v3.HttpConnectionManager
                stat_prefix: llm_gw
                route_config:
                  name: llm_routes
                  virtual_hosts:
                    - name: llm
                      domains: ["*"]
                      routes:
{routes}
                http_filters:
                  - name: envoy.filters.http.lua
                    typed_config:
                      "@type": type.googleapis.com/envoy.extensions.filters.http.lua.v3.Lua
                      default_source_code:
                        inline_string: |
                          local VALID_TOKEN = "{token}"
                          local MODEL_TO_TIER = {{
{model_map}
                          }}
                          function envoy_on_request(request_handle)
                            local auth = request_handle:headers():get("authorization")
                            if auth ~= ("Bearer " .. VALID_TOKEN) then
                              request_handle:respond({{[":status"] = "401"}}, '{{"error":"invalid or missing bearer token"}}')
                              return
                            end
                            local body_handle = request_handle:body()
                            if body_handle then
                              local body_str = body_handle:getBytes(0, body_handle:length())
                              local model = body_str:match('"model"%s*:%s*"([^"]+)"')
                              local tier = model and MODEL_TO_TIER[model]
                              if tier then
                                request_handle:headers():replace("x-model-tier", tier)
                              end
                            end
                          end
                  - name: envoy.filters.http.router
                    typed_config:
                      "@type": type.googleapis.com/envoy.extensions.filters.http.router.v3.Router

  clusters:
{clusters}
'''


def _resolve_mac_local_hostname(ctx: Ctx, m: dict) -> str:
    """The real mDNS/Bonjour hostname to use for `m` in a `.local` Envoy
    backend address -- not `m['hostname']` blindly, which is just
    inventory.yaml's human-chosen label (see resolve_local_mac's
    docstring). If `m` is the machine this script is running on, the value
    is already reconciled via resolve_local_mac() at startup. Otherwise
    this probes it over SSH, offers to fix inventory.yaml on a mismatch,
    and always returns the real name (even if the fix is declined) since
    the Envoy config must use whatever will actually resolve."""
    if m is ctx.local_mac:
        return m["hostname"]
    ip = m.get("ip")
    real = detect_remote_mac_hostname(ctx, m.get("ssh_user", "dev"), ip) if ip else None
    if real is None:
        print(f"Warning: couldn't verify {m['hostname']}'s real mDNS hostname over SSH -- using "
              f"inventory.yaml's label as-is. If that doesn't match this Mac's actual Local "
              f"Hostname (System Settings -> Sharing), '{m['hostname']}.local' won't resolve.")
        return m["hostname"]
    if real != m["hostname"]:
        print(f"Note: {m['hostname']} (inventory.yaml label) is actually named {real!r} on the network (via SSH scutil).")
        if confirm(ctx, f"Fix inventory.yaml: {m['hostname']!r} -> {real!r}?", default=True):
            old = m["hostname"]
            if _write_inventory_hostname(old, real):
                m["hostname"] = real
                print("inventory.yaml updated.")
            else:
                print(f"Couldn't safely auto-edit inventory.yaml -- change {old!r} to {real!r} by hand.")
    return real


def _build_envoy_backends(ctx: Ctx) -> tuple[list[dict], list[dict]]:
    """Returns (vllm_backends, ollama_backends) -- each a list of
    {name, address, port, health, cluster_type}.

    Server-class hosts (Linux boxes, Jetsons) get their inventory.yaml IP
    directly with cluster_type=STATIC -- those are assumed DHCP-reserved or
    otherwise stable, same as everywhere else in this platform.

    Laptops are a different case: a Mac's DHCP-assigned IP routinely changes
    (different wifi network, sleep/wake, lease renewal) in a way a rack-mounted
    Linux box's doesn't. Rather than bake in a raw IP that goes stale, Mac
    backends use `<hostname>.local` (mDNS/Bonjour -- built into macOS, nothing
    to install or run) with cluster_type=STRICT_DNS, which Envoy already
    re-resolves periodically. This only works for peers on the same LAN
    segment as the Mac, which is the same constraint mDNS always has.
    """
    vllm_backends, ollama_backends = [], []
    for h in ctx.inv.get("linux_gpu_hosts", []):
        if "vllm" in h.get("services", []):
            vllm_backends.append({"name": h["hostname"].replace("-", "_"), "address": h["ip"], "port": 8000,
                                   "health": "/health", "cluster_type": "STATIC"})
    for n in ctx.inv.get("jetson_nodes", {}).get("nodes", []):
        if "ollama" in n.get("services", []):
            ollama_backends.append({"name": n["hostname"].replace("-", "_"), "address": n["ip"],
                                     "port": ctx.inv["jetson_nodes"].get("ollama_port", 11434),
                                     "health": "/api/tags", "cluster_type": "STATIC"})
    for m in ctx.inv.get("mac_workstations", []):
        if "ds4-zgx-gb10" in m.get("services", []) and m.get("ram_gb", 0) >= 96:
            real_hostname = _resolve_mac_local_hostname(ctx, m)
            ollama_backends.append({"name": m["hostname"].replace("-", "_"), "address": f"{real_hostname}.local",
                                     "port": 8080, "health": "/v1/models", "cluster_type": "STRICT_DNS"})
    return vllm_backends, ollama_backends


def phase2_envoy(ctx: Ctx) -> None:
    section("Phase 2.1 — Envoy")
    gw = linux_cpu_gateway_host(ctx)
    require_real_value(gw["ip"], "ip", gw["hostname"])
    require_ssh(ctx, gw.get("ssh_user", "admin"), gw["ip"], gw["hostname"])

    vllm_backends, ollama_backends = _build_envoy_backends(ctx)
    if not vllm_backends and not ollama_backends:
        print("No host in inventory.yaml runs vllm or ollama -- nothing to route to. Skipping Envoy.")
        return

    print(f"Discovered from inventory.yaml: {len(vllm_backends)} vllm backend(s), "
          f"{len(ollama_backends)} ollama/ds4 backend(s).")
    token = ask("Bearer token for Envoy clients to present (this is a secret -- don't reuse one from elsewhere)", "sk-REPLACE-ME")

    clusters_yaml, routes_yaml, model_map_lines = [], [], []
    priority_order = []
    for b in vllm_backends + ollama_backends:
        clusters_yaml.append(f"""    - name: {b['name']}
      connect_timeout: 5s
      type: {b['cluster_type']}
      lb_policy: ROUND_ROBIN
      health_checks:
        - timeout: 3s
          interval: 10s
          unhealthy_threshold: 3
          healthy_threshold: 2
          http_health_check: {{ path: "{b['health']}" }}
      load_assignment:
        cluster_name: {b['name']}
        endpoints:
          - lb_endpoints:
              - endpoint:
                  address:
                    socket_address: {{ address: {b['address']}, port_value: {b['port']} }}""")
        priority_order.append(b["name"])
        routes_yaml.append(f"""                        - match:
                            prefix: "/"
                            headers:
                              - name: x-model-tier
                                string_match: {{ exact: "{b['name']}" }}
                          route: {{ cluster: {b['name']}, timeout: 300s }}""")
        model_name = ask(f"Model-name tier that should route straight to {b['name']} (blank = only reachable via failover)", "")
        if model_name:
            model_map_lines.append(f'                            ["{model_name}"] = "{b["name"]}",')

    clusters_yaml.append(f"""    - name: production_with_failover
      connect_timeout: 5s
      lb_policy: CLUSTER_PROVIDED
      cluster_type:
        name: envoy.clusters.aggregate
        typed_config:
          "@type": type.googleapis.com/envoy.extensions.clusters.aggregate.v3.ClusterConfig
          clusters: [{', '.join(priority_order)}]""")
    routes_yaml.append("""                        - match: { prefix: "/" }
                          route:
                            cluster: production_with_failover
                            timeout: 300s
                            retry_policy:
                              retry_on: "5xx,reset,connect-failure,refused-stream"
                              num_retries: 2
                              host_selection_retry_max_attempts: 3""")

    rendered = ENVOY_TEMPLATE.format(
        routes="\n".join(routes_yaml),
        token=token,
        model_map="\n".join(model_map_lines) if model_map_lines else "",
        clusters="\n\n".join(clusters_yaml),
    )

    local_tmp = REPO_ROOT / ".envoy.generated.yaml"
    local_tmp.write_text(rendered)
    print(f"Rendered config written locally to {local_tmp} for review before it goes anywhere.")

    if not confirm(ctx, f"Validate and deploy this config to {gw['hostname']} ({gw['ip']})?"):
        print("Left the rendered file at .envoy.generated.yaml -- nothing was deployed.")
        return

    validate = subprocess.run(
        ["docker", "run", "--rm", "-v", f"{local_tmp}:/etc/envoy/envoy.yaml:ro",
         "envoyproxy/envoy:v1.39.0", "--mode", "validate", "-c", "/etc/envoy/envoy.yaml"],
    )
    if validate.returncode != 0:
        raise AbortDeployment("envoy --mode validate failed on the rendered config -- not deploying. See .envoy.generated.yaml.")

    user, ip = gw.get("ssh_user", "admin"), gw["ip"]
    remote_base = ctx.inv["global"]["remote_base_path"]
    run_remote(ctx, user, ip, f"mkdir -p {remote_base}/envoy")
    run_local(f"scp {scp_identity_prefix(ctx)}{local_tmp} {user}@{ip}:{remote_base}/envoy/envoy.yaml")

    unit = (
        "[Unit]\\nDescription=Envoy - Unified Model Gateway\\nAfter=network.target docker.service\\n"
        "Requires=docker.service\\n\\n[Service]\\nType=simple\\n"
        "ExecStartPre=-/usr/bin/docker rm -f envoy-gateway\\n"
        f"ExecStart=/usr/bin/docker run --rm --name envoy-gateway -p 4000:4000 -p 127.0.0.1:9901:9901 "
        f"-v {remote_base}/envoy/envoy.yaml:/etc/envoy/envoy.yaml:ro envoyproxy/envoy:v1.39.0\\n"
        "ExecStop=/usr/bin/docker stop envoy-gateway\\nRestart=always\\nRestartSec=5\\n\\n"
        "[Install]\\nWantedBy=multi-user.target\\n"
    )
    if not confirm(ctx, "This restarts Envoy (drops in-flight connections for a few seconds) -- proceed?"):
        print("Config uploaded but not activated. Run the systemctl restart manually when ready.")
        return
    run_remote(ctx, user, ip, f"printf '{unit}' | sudo tee /etc/systemd/system/envoy-gateway.service >/dev/null && "
                          f"sudo systemctl daemon-reload && sudo systemctl enable --now envoy-gateway")
    print(f"Envoy live on {ip}:4000. Token: {token} -- store it somewhere real, it's not saved anywhere by this script.")


def phase2_rlmgw(ctx: Ctx) -> None:
    section("Phase 2.2 — rlmgw")
    gw = linux_cpu_gateway_host(ctx)
    user, ip = gw.get("ssh_user", "admin"), gw["ip"]
    require_real_value(ip, "ip", gw["hostname"])
    require_ssh(ctx, user, ip, gw["hostname"])

    upstream = ask("RLMGW_UPSTREAM_BASE_URL (one backend -- rlmgw is not a router)", ctx.inv["global"].get("rlmgw_url", ""))
    model = ask("RLMGW_UPSTREAM_MODEL", "<your-model-name>")
    repo_root = ask("RLMGW_REPO_ROOT (one codebase -- one rlmgw instance per active repo)", f"{ctx.inv['global']['remote_base_path']}/active-repo")

    if not confirm(ctx, f"Deploy rlmgw to {gw['hostname']} ({ip}) with these values?"):
        return

    remote_base = ctx.inv["global"]["remote_base_path"]
    run_local(f"rsync -avz --delete{rsync_ssh_flag(ctx)} --exclude '.git' --exclude '.venv' "
              f"'{ctx.inv['global']['local_repo_root']}/rlmgw/' '{user}@{ip}:{remote_base}/rlmgw/'")
    run_remote(ctx, user, ip, f"cd {remote_base}/rlmgw && python3.11 -m venv venv && "
                          f"venv/bin/pip install -e '.[gw]'")
    env_file = (
        "RLMGW_HOST=0.0.0.0\\nRLMGW_PORT=8010\\n"
        f"RLMGW_UPSTREAM_BASE_URL={upstream}\\nRLMGW_UPSTREAM_MODEL={model}\\n"
        f"RLMGW_REPO_ROOT={repo_root}\\nRLMGW_MAX_CONTEXT_PACK_CHARS=12000\\nRLMGW_SESSION_TTL_HOURS=24\\n"
    )
    run_remote(ctx, user, ip, f"printf '{env_file}' > {remote_base}/rlmgw/rlmgw.env")
    unit = (
        "[Unit]\\nDescription=RLMgw - Repo-Context Proxy for Coding Workloads\\nAfter=network.target\\n\\n"
        f"[Service]\\nType=simple\\nUser={user}\\nWorkingDirectory={remote_base}/rlmgw\\n"
        f"EnvironmentFile={remote_base}/rlmgw/rlmgw.env\\n"
        f"Environment=PATH={remote_base}/rlmgw/venv/bin:/usr/local/bin:/usr/bin\\n"
        f"ExecStart={remote_base}/rlmgw/venv/bin/python -m rlmgw.server --host ${{RLMGW_HOST}} "
        "--port ${RLMGW_PORT} --repo-root ${RLMGW_REPO_ROOT}\\nRestart=always\\nRestartSec=5\\n\\n"
        "[Install]\\nWantedBy=multi-user.target\\n"
    )
    run_remote(ctx, user, ip, f"printf '{unit}' | sudo tee /etc/systemd/system/rlmgw.service >/dev/null && "
                          f"sudo systemctl daemon-reload && sudo systemctl enable --now rlmgw")
    print(f"rlmgw live on {ip}:8010 -- health: curl -s http://{ip}:8010/healthz")


# ---------------------------------------------------------------------------
# Phase 4 — Security (aegis)
# ---------------------------------------------------------------------------

def phase4_aegis(ctx: Ctx) -> None:
    section("Phase 4.1 — aegis")
    targets = hosts_running(ctx, "aegis")
    if not targets:
        print("No host lists aegis. Skipping.")
        return
    print(f"aegis is assigned to {len(targets)} host(s). Cross-compiling from macOS isn't a solved path "
          f"(confirmed in the guide) -- this builds NATIVELY on each Linux/Jetson target over SSH, not locally.")
    for host in targets:
        name, ip, user, kind = host["hostname"], host["ip"], host["_ssh_user"], host["_kind"]
        if kind == "mac":
            print(f"\n{name} is a Mac -- build locally there yourself (`cargo build --release`); "
                  f"this script only automates the remote Linux/Jetson builds.")
            continue
        require_real_value(ip, "ip", name)
        require_ssh(ctx, user, ip, name)
        if not confirm(ctx, f"Build and install aegis natively on {name} ({ip})?"):
            continue
        remote_base = ctx.inv["global"]["remote_base_path"]
        run_local(f"rsync -avz --delete{rsync_ssh_flag(ctx)} --exclude '.git' --exclude 'target' "
                  f"'{ctx.inv['global']['local_repo_root']}/aegis/' '{user}@{ip}:{remote_base}/aegis/'")
        run_remote(ctx, user, ip, f"cd {remote_base}/aegis && cargo build --release")
        run_remote(ctx, user, ip, f"cd {remote_base}/aegis && sudo ./packaging/install-native.sh")
        run_remote(ctx, user, ip, "aegis --version", check=False)


# ---------------------------------------------------------------------------
# Phase 6 — ain-node
# ---------------------------------------------------------------------------

def phase6_ain(ctx: Ctx) -> None:
    section("Phase 6.3 — ain (P2P Agent Mesh)")
    targets = hosts_running(ctx, "ain")
    if not targets:
        print("No host lists ain. Skipping.")
        return
    bootstrap = ask("Bootstrap peer multiaddr (blank on the very first node; fill in once one node is up)", "")
    for host in targets:
        name, ip, user, kind = host["hostname"], host["ip"], host["_ssh_user"], host["_kind"]
        if kind == "mac":
            print(f"\n{name} is a Mac -- ain-node runs there via launchd, not systemd; "
                  f"see Phase 6.3's Mac block in the guide (this script covers Linux/Jetson only).")
            continue
        require_real_value(ip, "ip", name)
        require_ssh(ctx, user, ip, name)
        if not confirm(ctx, f"Build and deploy ain-node on {name} ({ip})?"):
            continue
        remote_base = ctx.inv["global"]["remote_base_path"]
        run_local(f"rsync -avz --delete{rsync_ssh_flag(ctx)} --exclude '.git' --exclude 'target' "
                  f"'{ctx.inv['global']['local_repo_root']}/ain/' '{user}@{ip}:{remote_base}/ain/'")
        run_remote(ctx, user, ip, f"cd {remote_base}/ain && cargo build --release")
        bootstrap_line = f"  --bootstrap {bootstrap} \\\\\\n" if bootstrap else ""
        unit = (
            "[Unit]\\nDescription=AIN Node - Decentralized Agent Mesh\\nAfter=network.target\\n\\n"
            f"[Service]\\nType=simple\\nUser={user}\\n"
            f"ExecStart={remote_base}/ain/target/release/ain-node --http-listen 0.0.0.0:8787 "
            f"--p2p-listen /ip4/0.0.0.0/tcp/4001{(' --bootstrap ' + bootstrap) if bootstrap else ''} "
            f"--data-dir {remote_base}/ain-data\\nRestart=always\\nRestartSec=10\\n\\n"
            "[Install]\\nWantedBy=multi-user.target\\n"
        )
        run_remote(ctx, user, ip, f"printf '{unit}' | sudo tee /etc/systemd/system/ain-node.service >/dev/null && "
                              f"sudo systemctl daemon-reload && sudo systemctl enable --now ain-node")
        print(f"ain-node live on {ip}:8787 / :4001 -- if this is the first node, grab its peer ID from "
              f"`curl -s http://{ip}:8787/v1/node/info` before deploying the next one's --bootstrap.")


# ---------------------------------------------------------------------------
# Phase 7.8 — local-harness
# ---------------------------------------------------------------------------

def phase7_local_harness(ctx: Ctx) -> None:
    section("Phase 7.8 — local-harness")
    lh_dir = pathlib.Path(ctx.inv["global"]["local_repo_root"]) / "local-harness"
    config_path = lh_dir / "config.json"
    if not config_path.exists():
        print(f"{config_path} not found -- run local-harness's own setup once first (see the guide).")
        return

    config = json.loads(config_path.read_text())
    print("This wires local-harness's 'local' lane at Envoy or rlmgw -- it only edits that one lane, "
          "your Claude/Gemini/Copilot lanes are left untouched.")
    choice = ask("Point the 'local' lane at 'envoy' or 'rlmgw'?", "envoy")
    if choice == "envoy":
        target = ctx.inv["global"].get("gateway_url", "")
        token = ask("Envoy bearer token (the one you set when deploying Envoy -- not stored anywhere by this script)")
    else:
        target = ctx.inv["global"].get("rlmgw_url", "")
        token = ""

    lane = next((l for l in config["lanes"] if l.get("id") == "local"), None)
    if lane is None:
        print("No lane with id 'local' in config.json -- add one manually via the Admin GUI first, "
              "this script only edits an existing lane.")
        return

    print(f"Current 'local' lane target: {lane.get('target')}")
    if not confirm(ctx, f"Change it to {target}?"):
        return
    lane["target"] = target
    if token:
        lane["apiKey"] = token
    config_path.write_text(json.dumps(config, indent=2))
    print(f"Updated. Restart local-harness (`./restart.sh` in {lh_dir}) for the change to take effect.")


# ---------------------------------------------------------------------------
# Guided-checklist mode — Phase 3, Phase 5, and the interactive installers.
# Deliberately does not execute anything on its own; see the module
# docstring for why.
# ---------------------------------------------------------------------------

GUIDED_CHECKLISTS: dict[str, list[str]] = {
    "3.1": [
        "Confirm the sandboxed-containers-operator + a KataConfig are already applied to your OpenShift "
        "cluster (`oc get kataconfig`) -- this script will not install a cluster operator for you.",
        "Build+push the fabrica image to your real registry Route (Prerequisites -> OpenShift Cluster).",
        "helm upgrade --install fabrica deploy/helm/fabrica -n ai-agents --create-namespace "
        "-f deploy/helm/fabrica/values-production.yaml --set image.repository=<your-registry>/fabrica "
        "--set image.tag=latest",
        "make helm-smoke",
    ],
    "5.1": [
        "Confirm this GPU host is NVIDIA (SDFT has no ROCm/HIP path) -- see the GPU Vendor Support Matrix.",
        "pip install torch --index-url https://download.pytorch.org/whl/cu124 (stable channel, not SDFT's "
        "own nightly-cu131 instructions -- those target GB10 hardware).",
        "Start an external-teacher vLLM server somewhere reachable.",
        "python3 main.py --output_dir <dir> --model_name_or_path Qwen/Qwen3-0.6B "
        "--vllm_server_base_url <teacher-url>   <-- this is a real training run, start it deliberately.",
    ],
    "5.2": [
        "Confirm this GPU host is NVIDIA, and be aware the custom kernels may not compile/run correctly "
        "outside Hopper-class hardware -- see the GPU Vendor Support Matrix's caveat.",
        "bash scripts/install.sh --full && python scripts/check_gb10_cuda.py --build-twell",
        "./launch.sh <n-gpus> sparsity_gated_1p5b zero1   <-- another real training run.",
    ],
    "6.1": [
        "oda.sh is fully interactive (9 blocking prompts, no working non-interactive flags) -- this "
        "cannot be scripted. Run: ssh -t <user>@<jetson-ip> \"cd .../oda && ./oda.sh\" and answer the "
        "prompts yourself.",
    ],
    "7.5": [
        "omarchy-ai is a one-time interactive Arch-only installer with a reboot in the middle -- run it "
        "manually, locally, on the machine actually being provisioned: "
        "curl -fsSL https://raw.githubusercontent.com/mitkox/omarchy-ai/main/boot.sh | bash",
    ],
}


def guided_checklist(ctx: Ctx, key: str) -> None:
    steps = GUIDED_CHECKLISTS.get(key)
    if not steps:
        print(f"No guided checklist for {key!r}.")
        return
    section(f"Guided checklist — {key} (not automated, see module docstring for why)")
    for i, step in enumerate(steps, 1):
        print(f"\n{i}. {step}")
        if not confirm(ctx, "Mark this step done and move to the next?"):
            print("Stopped here -- re-run this checklist to resume from the top.")
            return
    print(f"\n{key} checklist complete.")


# ---------------------------------------------------------------------------
# Menu
# ---------------------------------------------------------------------------

AUTOMATED: dict[str, tuple[str, callable]] = {
    "1.1": ("vllm (native)", phase1_model_serving),
    "1.2": ("ds4-zgx-gb10", phase1_ds4),
    "1.3": ("Ollama binding", phase1_ollama),
    "2.1": ("Envoy", phase2_envoy),
    "2.2": ("rlmgw", phase2_rlmgw),
    "4.1": ("aegis", phase4_aegis),
    "6.3": ("ain-node", phase6_ain),
    "7.8": ("local-harness", phase7_local_harness),
}


def print_list() -> None:
    print("Automated (runs real commands, with confirmation gates):")
    for key, (name, _) in AUTOMATED.items():
        print(f"  {key}  {name}")
    print("\nGuided checklist only (prints each step, confirms, never executes on its own):")
    for key in GUIDED_CHECKLISTS:
        print(f"  {key}  see DEPLOYMENT_GUIDE.md Phase {key}")
    print("\nEverything else in the guide (Phase 3.2-3.4, 6.2, 7.1-7.4, 7.6-7.7) has no entry here yet -- "
          "it's simple enough (a handful of flags, no host-fleet iteration) that the guide's own phase "
          "sections are the fastest path; open an issue/extend GUIDED_CHECKLISTS if you want one added.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--phase", help="e.g. '1' for all of phase 1, or '2.1' for just Envoy")
    parser.add_argument("--list", action="store_true", help="show what's automated vs. guided-only, then exit")
    parser.add_argument("--yes", action="store_true",
                         help="DANGEROUS: auto-confirm every prompt, including image builds, service "
                              "restarts, and remote deploys. Use only in a context where you've already "
                              "reviewed exactly what will run (e.g. re-running a phase you just did "
                              "interactively, to pick up an inventory.yaml edit).")
    args = parser.parse_args()

    if args.list:
        print_list()
        return 0

    inv = load_inventory()
    ctx = Ctx(auto_yes=args.yes, inventory=inv)
    ctx.local_mac = resolve_local_mac(ctx)

    targets: list[tuple[str, callable]] = []
    if args.phase:
        matches = [k for k in AUTOMATED if k == args.phase or k.startswith(f"{args.phase}.")]
        if matches:
            targets = [(k, AUTOMATED[k][1]) for k in matches]
        elif args.phase in GUIDED_CHECKLISTS:
            guided_checklist(ctx, args.phase)
            return 0
        else:
            print(f"Unknown phase {args.phase!r}. --list to see valid values.")
            return 1
    else:
        print("AI Engineering Platform — deployment orchestrator")
        print("Built from DEPLOYMENT_GUIDE.md's verified commands. See --list for scope.\n")
        for key, (name, fn) in AUTOMATED.items():
            targets.append((key, fn))

    for key, fn in targets:
        try:
            fn(ctx)
        except AbortDeployment as e:
            print(f"\n[{key}] stopped: {e}")
        except subprocess.CalledProcessError as e:
            print(f"\n[{key}] a command failed ({e}) -- stopping this component, moving on to the next.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
