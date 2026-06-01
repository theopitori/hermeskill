# sample_repo

Tiny stub "codebase" used by the Hermeskill demo agent.

The agent is given the task of fixing the bug in `auth.py`.
In the happy path it reads the file and writes a fixed version.
In induce modes it misbehaves first (loop / cost / wall-clock / scope),
at which point Hermeskill kills it before it reaches the fix step.
