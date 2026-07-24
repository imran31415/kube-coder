# Enabling the WhatsApp Conversation Gateway

Chat with your workspace's agent over WhatsApp. The gateway is **opt-in** and
**bring-your-own-credentials**: you supply your own Twilio or Meta app, and the
credentials are stored per-workspace on the PVC — never in the chart, a
ConfigMap, or an API response.

> Setup walkthroughs for each provider (creating the app, finding the
> credentials, registering templates) land with issue #332. This page covers
> **enabling the subsystem** and the **security posture**.

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

## 2. Connect a provider

Everything else is self-serve from the dashboard: **Settings → Messaging /
WhatsApp**. Pick a provider, paste your credentials, run **Test connection**,
copy the webhook URL into your provider console, then **Link WhatsApp** and text
the pairing code from your phone. The same card exists in the mobile app.

Saving credentials hot-swaps the live provider — no pod restart.

## 3. Security posture

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

## 4. Meta out-of-window templates

WhatsApp only allows free-form replies inside a 24-hour window. Outside it, a
provider-approved **template** is required.

Meta enforces template registration against **your own app**, so kube-coder's
built-in defaults are not sufficient there: until you register the template on
your Meta app and record the approval, the gateway **skips** the out-of-window
notification rather than attempting a send the provider would reject. The turn
still completes and the result is in the runner log.

Twilio has no such registry, so it keeps the built-in behavior.

## 5. Turning it off

Set `gateway.enabled: false` and redeploy: the external routes go inert and the
webhook ingress disappears. To revoke without disabling the subsystem, use
**Disconnect** in Settings (clears the stored credentials) or **Unlink** /
the `unlink` keyword (drops a bound number).
