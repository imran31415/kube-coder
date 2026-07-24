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

   Click **Save**, then **Test connection** — it makes a real Twilio API call and
   should report `connection ok`.
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
   `✅ Linked!` reply — your number is now bound (stored **hashed**, never in the
   clear).
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

## 6. Turning it off

Set `gateway.enabled: false` and redeploy: the external routes go inert and the
webhook ingress disappears. To revoke without disabling the subsystem, use
**Disconnect** in Settings (clears the stored credentials) or **Unlink** /
the `unlink` keyword (drops a bound number).
