# Launch copy

Ready-to-paste announcement text for Caspase. Not part of the package —
delete or keep as you like. Swap the demo GIF/asciinema link once recorded.

---

## Show HN

**Title** (HN caps title length ~80 chars; this fits):

```
Show HN: Caspase – an off-switch and autopsy for runaway AI agents
```

**Body:**

```
I kept watching agents melt money: a tool-call loop that never converges, a
context that balloons past the budget, a 40-minute wall-clock run that should
have been 40 seconds. The agent doesn't know it's stuck, and by the time I
notice, the bill is already spent.

Caspase is a supervisor that watches every tool call and LLM turn and
terminates the agent the moment it goes wrong — then writes a death
certificate you can audit. It checks for: identical-args call loops,
token/cost runaway, wall-clock overrun, out-of-scope tool calls, and
heartbeat loss. When one fires, the kill is cooperative and clean, and the
certificate records the symptom log, the shutdown sequence, and a one-click
"this kill was right / wrong" feedback link so you can tune the policy.

It ships today as a drop-in plugin for Hermes Agent — `pip install
caspase-hermes`, add `caspase` to plugins.enabled, done. The kill path uses
Hermes' canonical pre_tool_call block directive (hooks are non-blocking, so
you can't just raise out of the loop), which means the harm halts immediately:
no further tool runs, no further spend.

Design notes:
- The SDK never transmits tool arguments or transcripts. The loop detector
  compares *hashes* of arg tuples; only metadata (cost, token counts, model
  id) crosses the wire to the control plane.
- Control plane is a FastAPI service you run yourself (Postgres-backed).
  No phone-home; the SDK's only outbound HTTP is to your configured base URL.
- Policies are tiered (strict / coding-default / permissive) with loop, cost,
  and wall-clock caps you can override.

Honest caveats: it's a 0.1 alpha, Hermes is the only supported runtime today
(LangGraph and others are next), and in-process kills are cooperative —
an agent wedged in sync code won't notice until it hits the next await, so for
untrusted/long-running agents you still want to run them in their own
subprocess. The threat model and what it explicitly does NOT do are in
SECURITY.md.

Repo: https://github.com/theopidori/caspase
Would love feedback on the symptom set — what runaway mode have you hit that
these five checks would miss?
```

**First comment (post it yourself right after submitting):** a one-paragraph
"why I built this" + the demo GIF link. HN rewards the author engaging early.

---

## X / Twitter thread

**1/ (lead with the autopsy, attach the demo GIF)**

```
Your AI agent just spent $40 looping on the same tool call and nobody noticed.

Caspase is the off-switch. It watches every tool call + LLM turn, kills the
agent the moment it goes runaway, and hands you a death certificate.

pip install caspase-hermes

[demo.gif]
```

**2/**

```
Five things it watches for:
• identical-args call loops
• token / cost runaway
• wall-clock overrun
• out-of-scope tool calls
• heartbeat loss

Any one fires → clean kill → an auditable record of exactly why.
```

**3/**

```
The kill is honest. Hermes hooks are non-blocking, so Caspase uses the
canonical pre_tool_call block directive: the moment a check fires, no more
tools run, no more spend. The model is told to end the session.
```

**4/**

```
Privacy by construction: the SDK never ships your tool args or transcripts.
The loop detector compares *hashes* of arg tuples. Only metadata — cost,
tokens, model id — reaches the control plane, which you run yourself.
```

**5/ (CTA)**

```
0.1 alpha. Hermes Agent today, more runtimes next.

Repo + 5-min demo: https://github.com/theopidori/caspase

What runaway mode have you hit that these checks would've caught?
```

---

## Where to post

- **Show HN** — the launch. Tue–Thu, ~8–10am ET tends to land best.
- **X thread** — death-cert-first, GIF on tweet 1.
- **Nous Research / Hermes Discord** — most on-target audience; lead with
  "built a supervisor plugin for Hermes."
- **r/LocalLLaMA** and **r/AI_Agents** — same GIF, link to repo.
- A short launch blog post you can link from all of the above.
