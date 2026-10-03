# Security scanning

Check an app you are running in this workspace for security holes, before
anyone else finds them.

Scanning uses [Strix](https://github.com/usestrix/strix), an open-source AI
penetration tester. It is not a code reader: it runs your app, sends it real
attack traffic, and only reports a problem once it has managed to prove it.
That means a finding arrives with the steps to reproduce it, so you can see the
problem yourself rather than take a tool's word for it.

## What happens when you press Scan

```
  1. You pick an app        ┌──────────┐        ┌───────────┐
     that is running  ───►  │ scanner  │ ─────► │ your app  │
                            │          │ ◄───── │  :3000    │
  2. It tries to break in   └──────────┘        └───────────┘
     the way an intruder would: bad logins, injected input,
     unexpected requests — and watches how the app responds

  3. It proves what it finds, and writes down how

  4. You get a list, worst first, each with a reproduction
```

Findings appear while the scan is still running, so you can start reading
before it finishes.

## Before you can scan

**A model.** Scanning is done by an AI model that you choose and pay for.
Open **Security** in the dashboard and connect one: a model name in the
scanner's format (`provider/model-name`), and an API key from that provider.
A model running on this machine works too — give its address in the optional
server field.

Your key is stored in this workspace only, in the scanner's own settings file,
and is never shown again. It is kept separate from the key your assistant uses,
so a long scan cannot use up the budget the assistant depends on.

**Or a ChatGPT subscription instead of a key.** Enter a model name beginning
`chatgpt/` and the key field is replaced by a **Sign in with ChatGPT** button.
Because the sign-in has to finish in *your* browser rather than in the
workspace, it hands you a link: open it, sign in as usual, and the page you
land on will fail to load — that is expected. Copy the whole address from the
address bar and paste it back. Nothing keeps a copy of what you paste.

**A workspace that supports it.** The scanner runs its tools inside a
container, so the workspace has to be deployed with `strix.enabled: true` and
`build.mode: buildkit`. If it was not, the Security page says so rather than
failing at the first scan. See `charts/workspace/values.yaml`.

The scanner itself installs the first time you connect a model. That takes a
few minutes and happens once.

## Choosing how deep to go

| Depth | Roughly | Good for |
|---|---|---|
| Quick | 5 minutes | A fast pass before a demo or a deploy |
| Standard | 30–60 minutes | Routine checking |
| Deep | 1–4 hours | A thorough review, at a matching cost |

**Set a spending limit.** It is optional, but with no limit a deep scan runs
until it finishes and spends accordingly. The scan stops cleanly when it
reaches the limit you set. Live spend is shown while it runs.

## Reading the result

Findings are sorted worst first. Each one opens to show what it is, why it
matters, how to reproduce it, and how to fix it — in the scanner's own words.
Anything it reported that this page has no specific place for is still shown,
so nothing is quietly dropped.

**Dismiss** a finding that does not apply to you. That is recorded separately
from the scanner's own report, which is never edited.

A scan that finishes without finding anything says so, and says what it
checked. It never says only "0 problems" — a scan that could not reach your
app produces exactly the same empty list as a clean one, and the difference
matters.

## Things worth knowing

**Your app has to be reachable.** A dev server listening only on `127.0.0.1`
cannot be reached by the scanner, which runs in a container beside your
workspace. The app picker warns you when this is the case; restart the app
listening on `0.0.0.0` to fix it.

**Only scan what you own.** A scan sends genuine attack traffic. Targets are
limited to apps running inside your own workspace for exactly this reason.

**Your model provider sees the traffic.** Whatever the scanner sends to and
receives from your app passes through the model you chose, in order for it to
decide what to try next.

**Findings are sensitive.** They describe working attacks against your app.
They stay in this workspace, readable only by you.

**A scan can be watched and stopped from your phone.** The mobile app has the
same Security screen, and sends you a notification when a scan finds something.
A clean scan is recorded without notifying you.

## What is not covered yet

- **Scanning a folder of source code.** Only running apps can be scanned today.
- **Fixing a finding for you.** Handing a finding to an agent to fix is
  planned, not built.
