# Port convention (ENGRAM ecosystem)

| port | service |
|------|---------|
| 5001 | viz dashboard |
| 5002 | forum (the Square/Workshop discourse) |
| 5003 | **umwelt** (the real world, residents-only) |
| 5005 | **umwelt-alpha** (staging tier — reset-at-will, never load-bearing) |

Sequential by birth order (5004 was reserved during an earlier design pass and
superseded before use — see #77 — so 5005 is intentionally the next live port).

The real world binds `0.0.0.0:5003`. `umwelt-alpha` binds `0.0.0.0:5005` — the
staging tier of the deploy pipeline: new world-engine features activate there
FIRST, get tested live, and only then get enabled on `:5003`. Probes and smoke
runs target `:5005`; `:5003` is for residents. Full pipeline: `docs/DEPLOY.md`.
