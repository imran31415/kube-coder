# Triggers — webhooks

> **What a webhook trigger does.** Listens at a stable URL inside your
> workspace. When an external service POSTs a signed request, the
> workspace fires a Claude task with a prompt built from the payload.
> Use this to wire GitHub, Linear, Stripe, Slack, or any
> webhook-capable service into Claude.

Companion surface to crons (which fire on a schedule, not an event).
Both live under the **Triggers** tab.

## Anatomy of a webhook trigger

| Field | Purpose |
| --- | --- |
| **`id`** | URL-safe identifier — your endpoint becomes `/api/webhooks/{id}`. |
| **`prompt`** | Task prompt template. Use `{{ json.path.to.field }}` to interpolate payload. |
| **`workdir`** | Where the task runs (default `/home/dev`). |
| **`assistant`** | claude / openrouter / kc-harness. |
| **`signing_secret`** | HMAC-SHA256 secret. Required for non-test requests. |
| **`signing_kind`** | `generic-v0`, `github`, `slack`, `stripe`. Picks the signature header convention. |
| **`response_url`** | Optional — if set, the task's completion summary POSTs back here. |

The signing secret is generated when you create the trigger and shown
*once*. Save it on the calling side; the dashboard never displays it
again.

## Sign your request

For the default `generic-v0` kind:

```
X-Signature: v0=<hex(hmac_sha256(secret, f"{timestamp}.{body}"))>
X-Timestamp: <unix-seconds, within ±5 minutes>
```

For `github`, use what GitHub sends (`X-Hub-Signature-256`).
For `slack`, `X-Slack-Signature` + `X-Slack-Request-Timestamp`.
For `stripe`, `Stripe-Signature`.

The receiver does constant-time comparison and rejects:

- Bodies replayed within the 1024-entry LRU.
- Timestamps outside ±5 minutes.
- Anything missing a signature header.

## Prompt templating

The body is parsed as JSON and exposed to the prompt as `json`. Any
nested path works:

```text
{{ json.action }} on {{ json.pull_request.html_url }}.
Look at the changed files and post a review comment.
```

Missing paths render as the empty string, so a robust template can
absorb optional fields. The expanded prompt is what gets sent to
`claude`.

> :::scenario
> **Pattern: PR auto-review.**
> Create a GitHub webhook on `pull_request.opened`. Set the prompt to:
>
> > Review PR {{ json.pull_request.html_url }}. Check the diff,
> > comment on anything risky, approve only if it's safe.
>
> Every new PR fires a task; you watch results from the Build tab.
> :::

## Response URL

Set `response_url` and the workspace POSTs back when the task ends:

```json
{
  "task_id": "claude-2026-05-16-abc",
  "status": "completed",
  "summary": "Reviewed PR #42. No issues found.",
  "duration_ms": 38421
}
```

Useful for chaining workspaces or pinging Slack from your own bot. The
URL is HTTPS-only and explicit-port-scheme allowlisted (no `file:`,
`gopher:`, etc.).

## Test a trigger from the dashboard

The detail panel has a **Test** button — fires the prompt with a
synthetic payload you can paste in. Bypasses signature verification
(you're already authenticated to the dashboard) but uses the same
template path.

## Suspend / resume

The list view has a switch per trigger. Suspended triggers return 200
with `"suspended": true` and never spawn a task. Use this to silence
noisy services during a deploy without deleting the config.

## Run history

Every row has a **Runs** button. It opens the trigger's own ledger: one
entry per inbound call, newest first, with the time, the source, whether
the signature was actually verified, the outcome, and a link to the task
the call spawned.

Rejections are recorded too, which is the point — a POST with a bad HMAC
answers a deliberately vague `404`, so the ledger is the only place that
will tell you the signature was wrong rather than the id. Same for a
replayed body, a fire refused because the workspace was at its task
limit, and a cron firing with a token you have since rotated.

What is **not** recorded: the payload and the rendered prompt. The
ledger is metadata about the call; a webhook body is someone else's data
and often carries their credentials. Open the linked task to see what
the payload actually said.

The ledger is size-capped per trigger, so a busy webhook's oldest
entries age out rather than filling the disk, and deleting a trigger
deletes its history with it.

    GET /api/webhooks/<id>/runs?limit=50&offset=0
    GET /api/crons/<id>/runs
    GET /api/page-watches/<id>/runs

## Common failures

Open **Runs** on the trigger first — it says which of these happened,
and when.

- **`401 invalid signature`** — secret mismatch, or the timestamp is
  off by >5 minutes. Check the calling side's clock. Shows in Runs as
  `rejected` with **unverified**.
- **`409 replay`** — the same body+timestamp was seen recently. Bump
  the timestamp or change a field.
- **Prompt expands to gibberish** — `{{ json.x.y }}` paths don't
  match. Test with the dashboard's **Test** to see the expanded
  prompt.
- **Response URL doesn't fire** — task may have errored before
  finishing. Check the task detail for an error message.
