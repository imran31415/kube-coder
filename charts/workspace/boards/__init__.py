"""Board Processor — work items on an external tracker (#588/#589).

Three modules, split by purity because that split is what makes this testable:

- `schema`  — PURE. Validates a declarative connector config against a CLOSED
              schema. Knows nothing about HTTP or the filesystem.
- `engine`  — PURE given an injected HTTP callable. Fetches, paginates, maps to
              the canonical item shape, and runs multi-step actions. Never
              imports server.
- `limits`  — the three-tier rate limiter (global / per-action / per-item
              writes) that a connector declares as data.
- `store`   — IMPURE, and says so: atomic JSON records under a real `flock`.
              Compare-and-set is the primitive behind run leases and durable
              write budgets, and it cannot be faked in memory. Also `JsonlLog`,
              the append-only decision ledger, whose shape is the opposite:
              nothing is ever rewritten.
- `runs`    — the run state machine, leases, processed markers and selection.
- `review`  — dispositions, staged actions and the three approval guards.
- `templates` — PURE data. Starter connectors for GitHub, Jira and Zendesk.
              A template is a starting point, never a verified connector; only
              `test-fetch` earns that word.

The connector is DATA, never code: no eval, no plugin import, no generated
Python. An agent authors it at design time; this package executes it
deterministically at runtime, so no model is ever in the path of a board fetch
or write. `BoardsManager` in server.py owns everything impure — the PVC, the
credential store, READONLY_MODE and the event bus.
"""
