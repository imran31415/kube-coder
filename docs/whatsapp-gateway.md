# The WhatsApp Conversation Gateway

Chat with your workspace's agent over WhatsApp. The gateway is **opt-in** and
**bring-your-own-credentials**: you supply your own Twilio (or Meta) app, and the
credentials are stored per-workspace on the PVC — never in the chart, a
ConfigMap, or an API response.

This page is the end-to-end guide: **turn it on** (operator), **connect Twilio**
and **link your phone** (self-serve), plus the **security posture** and how to
turn it off. The whole connect flow is driven from **Settings → Messaging /
WhatsApp** in the dashboard (or the mobile app) — no `kubectl`.

## 1. Turn it on

The subsystem ships disabled. Enable it in your values overlay:

```yaml
gateway:
  enabled: true
```

With `enabled: false` (the default):

- `/api/gateway/*` external routes are inert (`503`, or a soft
  `{"available": false}` for the catalog/link list so the Settings section
  renders a clean "not available" state).
- `templates/ingress-gateway.yaml` — the public, OAuth-bypassing webhook path —
  **is not rendered at all**.
- The in-app **Walkie-Talkie** loopback preview (`/api/gateway/internal/*`) is a
  separate feature and keeps working either way.

The webhook ingress additionally requires `ingress.auth.type: oauth2`, since it
is the OAuth-bypass companion to that ingress.

## 2. Connect Twilio

Twilio's **WhatsApp Sandbox** is the fastest bring-your-own path — no number
approval, works in minutes. (For a production sender you'd register a WhatsApp
number with Twilio instead; the credentials below are identical.)

1. **Create a Twilio account** at [twilio.com](https://www.twilio.com/) and open
   the [Console](https://www.twilio.com/console).
2. **Open the WhatsApp Sandbox** — Console → **Messaging → Try it out → Send a
   WhatsApp message**. You'll see a sandbox sender number (e.g.
   `+1 415 523 8886`) and a **join code** (`join <two-words>`).
3. **Opt your phone in** — from WhatsApp on your phone, send `join <two-words>`
   to the sandbox number. (WhatsApp requires this opt-in before the sandbox can
   message you.)
4. **Grab your credentials** from the [Console dashboard](https://www.twilio.com/console):
   your **Account SID** and **Auth Token**.
5. **Enter them in kube-coder** — **Settings → Messaging / WhatsApp**, pick
   **Twilio**, and fill in:

   | Field | Value |
   |---|---|
   | **Account SID** | from the Console (starts `AC…`) |
   | **Auth Token** | from the Console (secret — stored redacted) |
   | **WhatsApp sender number** | `whatsapp:+14155238886` (your sandbox number) |

   The sender must be the **provider's** WhatsApp number in `+E164` form — not
   your own phone. Save normalizes display formatting (`+1 (415) 523-8886` →
   `whatsapp:+14155238886`) and **rejects** a value without a `+<country code>`
   (e.g. `(478) 347-7453`) — a sender like that would otherwise save silently
   and then fail every outbound send with a Twilio `400`.

   Click **Save**, then **Test connection** — it makes a real Twilio API call,
   checks the sender is a plausible `whatsapp:+E164` address, and should report
   `connection ok`.
6. **Point Twilio at your webhook** — copy the webhook URL shown in the Settings
   card (the **Copy** button next to it):

   ```
   https://<your-workspace-host>/api/gateway/whatsapp/webhook
   ```

   In Twilio, open the **Sandbox settings** tab and paste that URL into **"WHEN A
   MESSAGE COMES IN"**, method **HTTP POST**, then **Save**. Twilio signs each
   webhook over this exact URL, so paste it unchanged.

Saving credentials hot-swaps the live provider — no pod restart.

## 3. Link your phone and chat

Once Twilio is connected:

1. In **Settings → Messaging / WhatsApp**, click **Link WhatsApp**. You get a
   **6-digit pairing code** (valid ~10 minutes) and, for a dialable sender, an
   **Open in WhatsApp** shortcut.
2. **Text the code** to your WhatsApp sender number from your phone. You'll get a
   `✅ Linked!` reply — your number is now bound (keyed by its **hash** and
   never stored in the clear; §6 explains the sealed copy Board reviews use).
3. **Just chat.** Text the workspace normally; the agent drives your workspace and
   replies land back in WhatsApp.

### Keyword commands

Send any of these as the **whole message** (case-insensitive):

| Say | Effect |
|---|---|
| `new chat` · `new` · `start over` · `reset` | Start a fresh conversation thread |
| `stop` · `cancel` · `abort` | Stop the turn that's running |
| `unlink` · `disconnect` · `forget me` | Unbind this number from the workspace |
| `workspaces` · `list workspaces` | List the workspaces this number can reach |
| `@<ws> <message>` · `on <ws>: <message>` | Route this one message to a specific workspace |

### The 24-hour window

WhatsApp only permits **free-form** replies within **24 hours** of your last
inbound message. Inside the window the agent replies normally. Outside it, a
provider-approved **template** is required — see §5. The Twilio sandbox can't send
out-of-window templates, so a reply that lands after the window simply won't be
delivered until you message again.

## 4. Security posture

| Control | Behavior |
|---|---|
| Signature verification | **Fails closed.** Twilio `X-Twilio-Signature` and Meta `X-Hub-Signature-256` are verified in-pod; an unsigned or forged request gets `403`. |
| `gateway.allowUnsigned` | **Dev/sandbox only.** Accepts unsigned webhooks. The chart **refuses to render** when this is combined with TLS, so it can't reach production. Never emitted as env unless explicitly set. |
| Credentials at rest | JSON on the PVC, `0600`, atomic writes. Redacted in every API read — secret fields return only `set` + a last-4 hint, never the value. |
| Edge throttling | The webhook ingress carries `limit-rps` / `limit-connections` (`gateway.limits.webhookRps` / `webhookConnections`). |
| App throttling | Pairing-code minting and test-connection are capped per hour (`gateway.limits.linkPerHour` / `testPerHour`); the inbound webhook is additionally rate-limited per sender. |

```yaml
gateway:
  enabled: true
  allowUnsigned: false      # keep false outside local dev
  limits:
    linkPerHour: 10
    testPerHour: 20
    webhookRps: 5
    webhookConnections: 10
```

## 5. Meta out-of-window templates

Outside the 24-hour window (§3), a provider-approved **template** is required.

Meta enforces template registration against **your own app**, so kube-coder's
built-in defaults are not sufficient there: until you register the template on
your Meta app and record the approval, the gateway **skips** the out-of-window
notification rather than attempting a send the provider would reject. The turn
still completes and the result is in the runner log.

Twilio has no such registry, so it keeps the built-in behavior.

## 6. Board reviews on WhatsApp

When a Board item is waiting on a staged approval, the workspace sends it to
every paired number as well as to the app's push (#709). Push is still the main
channel; WhatsApp is for when the app isn't open.

**What arrives:**

| The proposal | The message |
|---|---|
| **One** comment of at most 700 characters | The **full text**, with **Approve** · **Reject** · **Open in app** |
| A longer comment, several writes, a status change, anything else | A one-line summary and a link to the review. No Approve button: nobody approves customer-visible text they can't read in full. |

A Zendesk-style `public` flag is spelled out ("public reply (the customer will
see it)" vs "internal note").

**What a tap does.** Each button carries the card's id, the ticket's content
hash and a signature made with a key only this workspace holds. A tap counts
only if the signature checks out, the card was sent to the number tapping, and
that number is still paired with a valid token. It then runs the same approval
as the app's Approve button:

- The ticket changed in the tracker, or the proposal was edited or replaced
  after the message went out: *"This ticket changed. Open the app to review."*
  Nothing is written.
- A second tap, or WhatsApp delivering the same tap twice: the reply is posted
  once; the second tap just says it was already approved.
- An unpaired number, or a payload that has been tampered with: nothing
  happens.

**Open in app** links to `https://<workspace host>/board?board=<id>&review=<item>`,
the same review a push opens. It is an https link because WhatsApp does not make
an app-scheme link tappable.

**Your number.** To start a conversation the workspace has to know your number,
so pairing now keeps it **sealed** in the link record (encrypted, with the key in
a separate `0600` file). It is still never stored in the clear and never shown
in any list. A number paired before this change is picked up the next time it
messages the workspace.

### Inside vs outside the 24-hour window

| | Inside the window | Outside the window |
|---|---|---|
| **Meta** | Interactive message with the three buttons | Your approved `board_review` / `board_review_link` template. If it isn't registered, nothing is sent. |
| **Twilio with a Content template** | The Content template | The Content template |
| **Twilio sandbox** | Numbered text: reply **1** to approve, **2** to reject, **3** for the link | Nothing (the sandbox can't send templates) |

On the sandbox a bare `1`/`2`/`3` counts only while the review card is still the
**last** message the workspace sent you. If the agent has sent anything since,
the digit goes to the agent as an ordinary chat message.

### Setting up the templates (one-time, done by the owner)

Templates have to be approved by Meta, which can take from minutes to a day, so
do this before you rely on it. The gateway uses two logical names; you record
the name (or SID) the provider gave you, the same way as the other templates in
§5.

**`board_review`** is the short card. Category **Utility**, any language.

```
{{1}} needs your review.

Proposed {{2}}:
{{3}}
```

Three **quick-reply** buttons, in this order: `Approve`, `Reject`, `Open in app`.
Example values for the review form: `SUP-46` / `comment` /
`Hi Dana, the refund was issued on the 3rd.`

**`board_review_link`** is for everything else. No buttons.

```
{{1}} needs your review: {{2}}. Open it here: {{3}}
```

`{{1}}` is the ticket key, `{{2}}` a summary such as "2 proposed writes
(comment, set_status)", and `{{3}}` the review link.

WhatsApp doesn't allow a line break inside a template value, so outside the
window the proposed text arrives on one line. Every word is kept; only the line
breaks are lost.

**Meta.** Create both templates in WhatsApp Manager. Once they're approved,
record each one on the workspace. Each quick-reply button's payload is set per
message, so the template needs no example payload.

```bash
mkdir -p /home/dev/.claude-triggers/gateway/templates
cat > /home/dev/.claude-triggers/gateway/templates/board_review.json <<'JSON'
{"name": "board_review", "provider_name": "kc_board_review",
 "language": "en_US", "status": "approved"}
JSON
cat > /home/dev/.claude-triggers/gateway/templates/board_review_link.json <<'JSON'
{"name": "board_review_link", "provider_name": "kc_board_review_link",
 "language": "en_US", "status": "approved"}
JSON
```

`provider_name` is the template name exactly as you created it in Meta, and
`language` is its language code.

**Twilio.** In **Content Template Builder**, create `board_review` as a
**Quick reply** template with the body above and three buttons whose **ids**
are variables: `Approve` → `{{4}}`, `Reject` → `{{5}}`, `Open in app` → `{{6}}`.
Create `board_review_link` as a **Text** template. Submit both for WhatsApp
approval, then record each with its **Content SID** (`HX…`) as `provider_name`:

```bash
cat > /home/dev/.claude-triggers/gateway/templates/board_review.json <<'JSON'
{"name": "board_review", "provider_name": "HXxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
 "language": "en", "status": "approved"}
JSON
```

A Twilio record without an `HX…` SID is ignored. Without one, Twilio behaves
like the sandbox: numbered text inside the window, nothing outside it.

Nothing needs restarting: the gateway reads these files each time it sends.

## 7. Turning it off

Set `gateway.enabled: false` and redeploy: the external routes go inert and the
webhook ingress disappears. To revoke without disabling the subsystem, use
**Disconnect** in Settings (clears the stored credentials) or **Unlink** /
the `unlink` keyword (drops a bound number).
