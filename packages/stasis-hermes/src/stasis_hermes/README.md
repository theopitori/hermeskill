# stasis-hermes

Apoptosis supervision for [Hermes Agent](https://github.com/NousResearch/hermes-agent). Stasis watches every tool call and LLM call in your Hermes session and kills the agent if it enters a runaway loop, exceeds cost/token caps, runs past a wall-clock deadline, or calls a tool it shouldn't.

## Install

```bash
pip install stasis-hermes
```

Then drop the plugin into Hermes:

```bash
python -c "
import stasis_hermes, pathlib, shutil
src = pathlib.Path(stasis_hermes.__file__).parent
dst = pathlib.Path.home() / '.hermes' / 'plugins' / 'stasis'
shutil.copytree(src, dst, dirs_exist_ok=True)
print('installed →', dst)
"
```

## Configure

```bash
export STASIS_API_KEY=sk-...
export STASIS_CONTROL_PLANE_URL=https://your-control-plane.example.com  # optional
export STASIS_AGENT_NAME=my-coding-agent   # optional display name
export STASIS_POLICY=coding-default        # optional policy
```

Or add these to `~/.hermes/.env`.

## Run

```bash
hermes
```

Stasis activates automatically. Every session appears in the Stasis dashboard.

## What it does

| Condition | What happens |
|-----------|-------------|
| Agent calls the same tool 5× in a row with identical inputs | Kill (loop) |
| Cumulative LLM cost exceeds policy cap | Kill (token_runaway) |
| Session runs longer than policy wall-clock cap | Kill (wall_clock) |
| Agent calls a tool not in the policy allowlist | Kill (tool_scope_violation) |
| Operator issues `stasis kill <agent_id>` | Kill (manual) |
| Operator issues a grant | Suppress one symptom type for up to 24 h |

On kill, Stasis posts a death certificate to the control plane with a full symptom log, shutdown sequence, and a one-click feedback URL so operators can label the kill verdict.

## Policies

Policies live in the Stasis control plane. Defaults shipped with the SDK:

| Policy | Loop cap | Cost cap | Wall-clock cap |
|--------|----------|----------|----------------|
| `coding-default` | 5 repeats / 20 actions | $2.00 | 30 min |
| `coding-permissive` | 8 repeats / 40 actions | $10.00 | 2 h |

## Operator CLI

```bash
stasis agents list
stasis logs <agent_id>
stasis kill <agent_id> --reason "infinite loop in file search"
stasis grants create <agent_id> --symptom loop --duration 1h --reason "known flaky task"
```
