# Third-Party Notices

This repository is a deployment guide, an inventory schema, and two small
orchestration scripts ([DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md),
[deploy-platform.py](deploy-platform.py), [generate-env.py](generate-env.py),
[inventory.yaml](inventory.yaml)) — all original work by this repo's author,
covered by [LICENSE](LICENSE) (MIT).

**None of the 26 components this guide deploys are vendored here.** Each one
is a git submodule: a pointer (remote URL + commit SHA) to its own,
independently-owned public repository, not a copy of its source. `LICENSE`
only covers the guide and the two scripts — it has no bearing on any
submodule, each of which is governed entirely by its own license, at its own
repository, set by its own author. Cloning this repo does not check out
submodule contents by default:

```bash
git clone --recurse-submodules <this-repo-url>
# or, after a plain clone:
git submodule update --init --recursive
```

## Components and their licenses

| Component | Upstream repository | License |
|---|---|---|
| SDFT | [github.com/mitkox/SDFT](https://github.com/mitkox/SDFT) | Apache-2.0 |
| SkillOpt | [github.com/mitkox/SkillOpt](https://github.com/mitkox/SkillOpt) | MIT |
| Thinking-with-Visual-Primitives | [github.com/mitkox/Thinking-with-Visual-Primitives](https://github.com/mitkox/Thinking-with-Visual-Primitives) | Dual — see note below |
| aegis | [github.com/mitkox/aegis](https://github.com/mitkox/aegis) | MIT |
| ai-coding-factory | [github.com/mitkox/ai-coding-factory](https://github.com/mitkox/ai-coding-factory) | MIT |
| aimatch | [github.com/mitkox/aimatch](https://github.com/mitkox/aimatch) | MIT |
| ain | [github.com/mitkox/ain](https://github.com/mitkox/ain) | MIT |
| aks-edge-utils | [github.com/mitkox/aks-edge-utils](https://github.com/mitkox/aks-edge-utils) | MIT (fork of Microsoft's AKS Edge Essentials; not referenced by this platform's `inventory.yaml` or guide) |
| background-coding-agents | [github.com/mitkox/background-coding-agents](https://github.com/mitkox/background-coding-agents) | MIT |
| ccar | [github.com/mitkox/ccar](https://github.com/mitkox/ccar) | MIT |
| dede | [github.com/mitkox/dede](https://github.com/mitkox/dede) | MIT |
| ds4-zgx-gb10 | [github.com/mitkox/ds4-zgx-gb10](https://github.com/mitkox/ds4-zgx-gb10) | MIT |
| fabrica | [github.com/mitkox/fabrica](https://github.com/mitkox/fabrica) | Apache-2.0 |
| firecracker-agentfs | [github.com/mitkox/firecracker-agentfs](https://github.com/mitkox/firecracker-agentfs) | MIT |
| fteplusai | [github.com/mitkox/fteplusai](https://github.com/mitkox/fteplusai) | MIT |
| llama3.2-coreml | [github.com/mitkox/llama3.2-coreml](https://github.com/mitkox/llama3.2-coreml) | Apache-2.0 |
| local-harness | [github.com/aidotse/local-harness](https://github.com/aidotse/local-harness) | Custom Source Available — see note below |
| megacode | [github.com/mitkox/megacode](https://github.com/mitkox/megacode) | MIT |
| oda | [github.com/mitkox/oda](https://github.com/mitkox/oda) | MIT |
| oda-r | [github.com/mitkox/oda-r](https://github.com/mitkox/oda-r) | MIT |
| omarchy-ai | [github.com/mitkox/omarchy-ai](https://github.com/mitkox/omarchy-ai) | MIT |
| rlmgw | [github.com/mitkox/rlmgw](https://github.com/mitkox/rlmgw) | MIT |
| sonic | [github.com/mitkox/sonic](https://github.com/mitkox/sonic) | MIT |
| sparser-faster-llms | [github.com/mitkox/sparser-faster-llms](https://github.com/mitkox/sparser-faster-llms) | MIT |
| tnt | [github.com/mitkox/tnt](https://github.com/mitkox/tnt) | MIT |
| vllm-turboquant | [github.com/mitkox/vllm-turboquant](https://github.com/mitkox/vllm-turboquant) | Apache-2.0 (fork of [vllm-project/vllm](https://github.com/vllm-project/vllm), itself Apache-2.0) |

All licenses above were read directly from each repository's own `LICENSE`
file at the commit this repo currently pins to (`git submodule status`) —
not inferred from a README or assumed. Re-check the pinned commit's actual
license before relying on this table; a submodule bump could change it.

## Special cases

**Thinking-with-Visual-Primitives** ships two separate license files, not
one: `LICENSE-CODE` (MIT, copyright DeepSeek — this component is a DeepSeek
research artifact mirrored under the `mitkox` account, not `mitkox`'s own
work) and `LICENSE-MODEL` (the DeepSeek Model License, a use-restricted
license covering the accompanying model weights/paper, not source code).
Treat the two independently — MIT terms for code, the DeepSeek Model License
terms for anything derived from the model itself.

**local-harness** is also an AI Sweden project — the same organization
publishing this repository — so referencing it here raises no cross-org
question the way it would for a genuinely external dependency. It's still
licensed independently under a Custom Source Available License, not MIT:
personal, non-commercial use is unrestricted, while use by any other company,
organization, or institution requires AI Sweden's prior written agreement.
That applies to whoever clones this repo and wants to use local-harness on
its own — this repository itself still only holds a submodule pointer to it,
not a copy of its code, same as every other component above.
