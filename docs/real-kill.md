# Killing a real Hermes agent (GPT-4o)

The [`python -m demo`](offline-demo.md) showpiece is deterministic and offline
on purpose — it proves the *engine* with no API key. This page is the other
half: Hermeskill killing a **real**
[Hermes Agent](https://github.com/NousResearch/hermes-agent) session driving
GPT-4o, with nothing scripted. Same engine, real runtime, real model spend.

Everything below is a verbatim run. The only setup is a clone, `uv sync`,
enabling the plugin, and four env vars.

## Setup

```powershell
git clone https://github.com/theopitori/hermeskill.git
cd hermeskill
uv sync

# Enable the Hermeskill plugin in Hermes' config (pip/entry-point plugins are
# enabled via the config key, not `hermes plugins enable`).
uv run python -c @"
from hermes_cli.config import load_config, save_config
cfg = load_config()
enabled = cfg.setdefault('plugins', {}).setdefault('enabled', [])
if 'hermeskill' not in enabled:
    enabled.append('hermeskill')
    save_config(cfg)
print('plugins.enabled =', enabled)
"@
# → plugins.enabled = ['hermeskill']

$env:HERMESKILL_API_KEY  = "sk_dev_developer_local_only_do_not_ship"
$env:HERMESKILL_BASE_URL = "http://localhost:8000"
$env:HERMESKILL_POLICY   = "strict"   # tight caps so the kill fires fast
```

(The control plane runs in a separate terminal — see the README
["Advanced: supervising a real runtime"](../README.md#advanced-supervising-a-real-runtime-hermes)
section for the full two-terminal walkthrough.)

## The run

A prompt engineered to make the agent loop on one tool with identical args:

```powershell
uv run hermes chat -q "Read this repo's README.md six times in a row using the read_file tool, with the exact same args every call. Do not skip any. Do not summarise between calls."
```

Hermes starts obeying — then Hermeskill pulls the plug on the **3rd** identical
call (the `strict` policy caps identical tool calls at 3):

```text
Query: Read this repo's README.md six times in a row using the read_file tool …
Initializing agent...
────────────────────────────────────────

  ┊ 📖 preparing read_file…
  ┊ 📖 preparing read_file…
  ┊ 📖 preparing read_file…
  ┊ 📖 read      README.md  1.7s
  ┊ 📖 preparing read_file…
  ┊ 📖 read      README.md  0.0s
  ┊ 📖 preparing read_file…

╭─ ⚕ Hermes ───────────────────────────────────────────────────────────────────╮
   The Hermeskill supervisor has terminated this session because the read_file tool
   was called with identical arguments three consecutive times, exceeding the
   configured limit. This runaway behavior triggered an automatic shutdown to
   prevent further loops. Please review and adjust your approach or policy
   settings before retrying.
╰────────────────────────────────────────────────────────────────────────────────╯

Session:        20260531_073915_aa1195
Duration:       21s
Messages:       8 (1 user, 6 tool calls)
```

## The autopsy

The kill is recorded on the control plane like any other — visible to the
operator CLI:

```text
PS> uv run hermeskill fleet
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━┓
┃ ID                                   ┃ Name   ┃ Policy ┃ Status     ┃ Last HB ┃ Registered ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━┩
│ fa4ed569-4257-4377-81a3-4e44fda65982 │ hermes │ strict │ terminated │       - │   10:39:14 │
└──────────────────────────────────────┴────────┴────────┴────────────┴─────────┴────────────┘

PS> uv run hermeskill logs fa4ed569-4257-4377-81a3-4e44fda65982
10:39:36 lifecycle registered agent_id=fa4ed569-… offline=False
10:39:36 lifecycle llm_start model=gpt-4o
10:39:36 llm       gpt-4o in=9545 out=107 $0.0249
10:39:36 tool      read_file
10:39:36 lifecycle tool_end tool=read_file
10:39:36 llm       gpt-4o in=7667 out=16 $0.0193
10:39:36 tool      read_file
10:39:36 lifecycle tool_end tool=read_file
10:39:36 llm       gpt-4o in=198 out=16 $0.0007
10:39:36 tool      read_file
10:39:36 symptom   loop (terminal) signature 'read_file|f969022d650c7957' repeated 3x in last 3 actions (cap 3)
10:39:36 llm       gpt-4o in=158 out=57 $0.0010
10:39:36 lifecycle session_end
```

The `symptom loop (terminal)` line is the same verdict the offline demo
produces — only here it fired against a live GPT-4o agent, with real token
counts and real cost, and Hermeskill surfaced the block to Hermes as a tool error
that ended the session cooperatively.

> Tested against `hermes-agent==0.14.0`. Hermeskill attaches via the standard
> `hermes_agent.plugins` entry-point and the `pre_tool_call` block directive —
> see the README ["Advanced: supervising a real runtime"](../README.md#advanced-supervising-a-real-runtime-hermes)
> section to reproduce.
