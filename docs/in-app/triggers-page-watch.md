# Triggers — page watch

> **What a page watch does.** Checks a web page on a schedule and runs
> your prompt only on the checks where the content actually changed.
> Nothing happens on the checks where the page looks the same. Use this
> to stop refreshing a tab — "tell me when this CI page goes green" is
> the one it was built for.

Companion surface to webhooks (which fire on an inbound request) and
crons (which fire every time the clock says so). All three live under
the **Triggers** tab.

## Anatomy of a page watch

| Field | Purpose |
| --- | --- |
| **`id`** | Lowercase letters, digits and hyphens, max 40 — it becomes part of a Kubernetes CronJob name. |
| **`url`** | The page to watch. Public `http(s)` addresses only. |
| **`selector`** | Optional CSS selector. Narrows the watch to one part of the page. |
| **`schedule`** | How often to check, in cron syntax. `*/5 * * * *` is every five minutes. |
| **`prompt`** | What Claude should do when the page changes. |
| **`workdir`** | Where the task runs (default `/home/dev`). |
| **`include page text`** | Off by default. See *What the prompt is told* below. |

## The first check is silent

Creating a watch does not start anything. The first check records what
the page looks like now — the baseline — and fires nothing. Only a
*later* check that finds different content runs your prompt.

That means a brand-new watch reads **waiting for first check** until its
schedule comes round. Use **Check now** if you don't want to wait.

Each change fires exactly once. The watch does not keep firing while the
page stays on its new content.

## Watching a CI build

This version reads the page as the server sends it and does **not** run
the page's JavaScript. Most CI dashboards draw their status in
JavaScript, so watching the dashboard URL often sees nothing useful.

Watch the **status badge** instead. It is a small image whose text is
server-rendered, so a plain read sees it:

```text
https://github.com/<owner>/<repo>/actions/workflows/ci.yml/badge.svg
```

That image literally contains the word `passing` or `failing`, so the
watch sees the flip the moment CI finishes. Most CI systems publish an
equivalent badge.

> :::scenario
> **Pattern: tell me when the build goes green.**
> Watch your workflow's `badge.svg` every five minutes with the selector
> `text`, and prompt *"CI finished — check whether the failure I was
> chasing is fixed, and summarise what changed."* You get one build when
> the badge flips, and silence the rest of the day.
> :::

## Use the selector to kill noise

Most pages change constantly in ways you don't care about: a clock in
the header, a rotating ad, a "generated at" line in the footer. Without
a selector, any of those counts as a change.

The selector is your noise control. Point it at the one element that
matters and everything else on the page becomes invisible to the watch.

Supported: `tag`, `#id`, `.class`, `[attr="value"]`, several of those on
one element (`div.card#main`), and descendant chains separated by spaces
(`.build .status`).

Not supported: `>`, `+`, `~`, `:pseudo-classes`, and comma-separated
lists. An unsupported selector is refused when you save, not silently
later.

The watch also ignores a lot of noise on its own: it compares only the
page's **visible words**. Tag attributes, scripts, styles, comments and
whitespace changes are all discarded before comparison, so a re-render
that changes a session token or reflows the HTML does not wake it.

## What the prompt is told

By default the prompt gets **metadata only** — the URL, when the change
was seen, and a content hash. It does not get the page's own words.

That default is deliberate. A watched page is written by someone else,
and its text would be read by an agent that acts on what it says. Tick
**Include page text in the prompt** if you want an excerpt; it is capped,
and it is scanned for hidden instructions first. If the scan finds
characters that are invisible to you but readable by a model, the
excerpt is dropped and the prompt says so rather than passing it along.

## Addresses that are refused

A watch must point at a genuinely public address. These are rejected
when you save, with an error:

- `localhost` and `127.0.0.1`
- private ranges like `10.x` and `192.168.x`
- `169.254.169.254` — the cloud metadata service
- anything inside the cluster, such as a `.svc.cluster.local` name

The same check runs again on every scheduled fetch, because DNS can
change between saving a watch and using it.

## Redirects

If the URL you paste redirects, the redirect is followed **once, when you
save**, and the final address is stored and shown back to you. You never
have to hunt for it yourself.

After that the watch reads that final address directly and does not
follow redirects. If the page later starts redirecting, that is reported
as a failed check rather than followed — the thing you chose to watch has
moved, and you should know.

## Suspend / resume

Pause stops the checks without deleting the watch, and keeps the
baseline. Resuming picks up where it left off — if the page changed while
paused, the next check sees it and fires once.

## Common failures

- **`that address is not publicly reachable`** — the URL points at
  loopback, a private range, the metadata address, or an in-cluster
  service. Watch a public address.
- **`the selector '…' matched nothing on this page`** — the page was
  restructured and your selector no longer matches. The watch keeps its
  old baseline and fires nothing until you fix the selector.
- **`the page now redirects`** — the page moved. Recreate the watch on
  the new address.
- **`response hit the size ceiling and was truncated`** — the page is
  too large to compare reliably. Narrow it with a selector.
- **`check failed`** on the row — the last fetch errored (timeout, 5xx,
  DNS). Failures never fire your prompt and never overwrite the
  baseline, so a temporary outage does not produce a false "changed"
  when the page comes back.
