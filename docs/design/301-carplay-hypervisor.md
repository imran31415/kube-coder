# Spike: Hypervisor chat on Apple CarPlay (hands‑free voice chat while driving)

> Research / design spike for [#301](https://github.com/imran31415/kube-coder/issues/301).
> **No product code changes** — this document picks an approach, checks it against
> Apple's constraints, and produces a phased plan + effort estimate. Every
> backend/mobile claim is grounded in a real file + line reference; every Apple /
> tooling claim is grounded in a source in [§11](#11-sources).

---

## 0. TL;DR

- **Recommendation: ship Path A (Communication / SiriKit Messaging) as the MVP,
  design the voice layer to be category‑agnostic, and migrate to Path B (the new
  iOS 26.4 voice‑assistant category) once its entitlement + template APIs are
  publicly documented and we can test them.** Path A is battle‑tested (it's how
  WhatsApp/Signal work in the car), works on shipping iOS, and reuses primitives
  we'll need either way (APNs, a speakable rendering, a "which workspace" picker).
  Path B is the more *honest* model for an AI agent but is brand‑new (guide
  updated 9 Feb 2026), thinly documented, iOS‑26.4‑only, and its entitlement key
  and template classes are **not yet in retrievable public docs** — too much
  unknown to commit an MVP to.
- **This is not a small change.** It forces the mobile app out of pure‑managed
  Expo into a **prebuild + config‑plugin** workflow (native Swift), adds an
  **Intents App Extension**, adds **APNs** (new client *and* server work), and is
  gated by an **Apple‑granted CarPlay entitlement** on our App ID before EAS can
  even sign a build. Rough end‑to‑end MVP estimate: **~5–8 engineer‑weeks**, most
  of it native‑iOS and Apple‑review latency, not JS.
- **Backend impact is modest but real:** the existing `/api/hypervisor/*` facade
  already covers "read status / read last message / send a message." The gaps are
  **unread tracking**, a **speakable (TTS‑friendly) rendering** of turns, a
  **cheap "latest activity across all threads" endpoint**, and (for
  announcements) an **APNs push‑token registry + sender**. All land in
  `server.py` / `hypervisor_session.py`.

---

## 1. How the system works today (grounded)

Understanding the current transport and data model is what makes the CarPlay
constraints tractable — so this section is the factual base for everything below.

### 1.1 The Hypervisor chat API

Threads are structured agent sessions. The mobile client talks to the same
`/api/hypervisor/*` facade as the dashboard SPA — a canonical **event stream**,
no terminal scraping (`mobile/src/screens/HypervisorScreen.tsx:1-7`).

Client methods (`mobile/src/api/client.ts`):

| Purpose | Function | Endpoint | Line |
|---|---|---|---|
| Chat config (enabled, default assistant, workdir) | `getHypervisorConfig()` | `GET /api/hypervisor/config` | `client.ts:645` |
| List threads | `listThreads()` | `GET /api/hypervisor/threads` | `client.ts:659` |
| Create thread (+ first message) | `createThread()` | `POST /api/hypervisor/threads` | `client.ts:680` |
| Thread detail + event stream | `getThreadDetail(id, since)` | `GET /api/hypervisor/threads/{id}?since=` | `client.ts:692` |
| Send a follow‑up message | `sendThreadMessage(id, msg)` | `POST /api/hypervisor/threads/{id}/messages` | `client.ts:698` |
| Stop the running turn | `stopThread(id)` | `POST /api/hypervisor/threads/{id}/stop` | `client.ts:713` |

Server handlers mirror these one‑for‑one in `charts/workspace/server.py`:
`handle_hypervisor_config` (`server.py:5093`), `..._list_threads`
(`server.py:5105`), `..._create_thread` (`server.py:5119`), `..._get_thread`
(`server.py:5150`), `..._send_message` (`server.py:5203`), `..._stop`
(`server.py:5247`). There is also a per‑thread **activity** timeline
(`handle_hypervisor_get_activity`, `server.py:5172`) and a global **health**
snapshot (`handle_hypervisor_health`, `server.py:5192`).

### 1.2 The event schema (what a "turn" is made of)

Canonical events are one JSON object per line in `events.jsonl`, documented at
`charts/workspace/hypervisor_session.py:26-39`:

```
seq  : int    (monotonic per thread, starts at 1)
ts   : float  (unix seconds)
role : "user" | "assistant" | "system"
type : "message" | "tool_call" | "tool_result" | "error" | "status" | "choice"
  message     -> text
  tool_call   -> tool:{name,input}, tool_id
  tool_result -> tool_use_id, text, is_error
  status      -> status: "running"|"idle"|"error"   (turn lifecycle)
  choice      -> options:[...], question             (clickable picker)
```

The mobile `buildTurns()` folds this into user bubbles + agent turns of prose +
expandable tool‑activity chips (`HypervisorScreen.tsx:374`, `util/hvTranscript`).
**This is the crux for voice:** a turn is prose *interleaved with tool‑call
transcripts*, which are not speakable as‑is (§3.3).

### 1.3 Thread metadata / status

`HypervisorSession.summary()` (`hypervisor_session.py:1118-1130`) returns exactly:
`id, title, assistant, status, created_at, updated_at, deleted_at`. Status is
`running | idle | error` (`hypervisor_session.py:1099-1104`).

> **Gap for CarPlay:** there is **no unread state, no last‑message text, and no
> per‑thread "last agent message" preview** in the summary. A voice UX that says
> "you have a new reply from the deploy chat" needs at least one of these
> (§3, §4).

### 1.4 Transport: polling + Bearer, no push, no SSE

- Every request attaches `Authorization: Bearer <token>` against a
  user‑supplied `host` (`client.ts:78-114`). Host + token are operator‑supplied,
  never hardcoded.
- **SSE is intentionally not used** — `EventSource` can't send an auth header, so
  screens **poll** (`client.ts:10-14`). The Hypervisor screen polls the open
  thread every **2 s** (`HypervisorScreen.tsx:158`).
- There is **no WebSocket, no APNs, no background delivery** anywhere in the tree.

### 1.5 Mobile app shape (Expo managed)

`mobile/app.config.ts` is a **managed** Expo SDK 56 config. Plugins are
`expo-secure-store`, `expo-video`, `expo-build-properties`, `expo-splash-screen`,
`expo-image-picker` (`app.config.ts:50-79`). iOS `infoPlist` carries only
`ITSAppUsesNonExemptEncryption` and an ATS exception for self‑hosted HTTP hosts
(`app.config.ts:24-34`). **There is no native Xcode project, no SiriKit, no App
Intents, no CarPlay, and no push** — only photo/camera perms. Builds run through
EAS (`mobile/eas.json`); **binary builds are EAS's job and Fastlane only pushes
store *listing metadata*** (`mobile/fastlane/Fastfile:1-4`).

---

## 2. Apple's constraints (confirmed) and the two paths

CarPlay does **not** allow arbitrary apps or custom chat UIs. Apple grants **one
entitlement category per app** and you may only use that category's templates —
the entitlement is granted to your developer account / App ID *before* you can
build or sign ([Newly.app][newly], [Apple CarPlay Developer Guide][guide]). The
real entitlement keys are **hyphenated**, e.g. `com.apple.developer.carplay-communication`
— *not* the dotted `...carplay.communication` form (the research corrected this;
[Apple entitlement doc][ent-comm]).

Two categories are relevant:

### Path A — Communication category (the "like iMessage" path)

- Entitlement `com.apple.developer.carplay-communication` (with the
  `carplay-messaging` sub‑capability), plus `com.apple.developer.siri` and
  `NSSiriUsageDescription` ([Apple forum thread][forum], [entitlement doc][ent-msg]).
- **Voice‑only via Siri; no custom chat screen.** Tapping the app in CarPlay
  launches Siri to read/compose; the app draws no message list ([guide][guide]).
- Requires the SiriKit **Messaging** intents in an **Intents App Extension**
  (declared under `NSExtension → IntentsSupported`, *not* the main target):
  - `INSendMessageIntent` — dictate & send a reply
  - `INSearchForMessagesIntent` — Siri reads unread/incoming
  - `INSetMessageAttributeIntent` — mark as read
  — the same set WhatsApp/Signal implement ([forum][forum], [guide][guide]).
- Incoming messages are surfaced via **user notifications** (so Siri has
  something to announce) → in practice this needs **APNs** (§4).
- **Trade‑off:** maps cleanly onto person‑to‑person messaging, but the Hypervisor
  is an **AI agent, not a contact** — each thread has to masquerade as "a
  conversation with a person."

### Path B — Voice‑driven conversational app category (new, iOS 26.4 / Feb 2026)

- Apple's CarPlay Developer Guide update of **9 Feb 2026** added a dedicated
  entitlement category for third‑party **voice/AI‑assistant** apps — an official
  path to run a conversational agent in CarPlay alongside Siri
  ([AppleInsider][ai]).
- Reported constraints ([AppleInsider][ai]): app must **launch directly into
  voice interaction**; **max 3 template screens** incl. root; **no custom UI**
  (unsupported templates throw at runtime); audio session **active only during
  voice use**; **iOS 26.4+**; entitlement Apple‑gated + App Store review; **cannot
  control vehicle functions**.
- **Trade‑off:** the honest model for an AI agent, but newest / least‑documented,
  higher review uncertainty, iOS‑26.4 floor, and — importantly — **the
  entitlement key string and template class names are not yet in retrievable
  public docs** (the research could not confirm them; [§11](#11-sources)).

---

## 3. Deliverable 1 — Path A vs B recommendation + eligibility

### 3.1 Recommendation

**Ship Path A first; keep the voice layer category‑agnostic; migrate to Path B
when it's documented and testable.** Rationale:

| Dimension | Path A (Communication) | Path B (Voice assistant) |
|---|---|---|
| Maturity / docs | Mature, well‑documented, many shipping apps | Brand‑new (Feb 2026), key + templates **undocumented** |
| iOS floor | Shipping iOS (SiriKit Messaging is old) | **iOS 26.4+** only |
| Conceptual fit | Awkward — agent‑as‑contact | **Natural** — agent‑as‑assistant |
| Review risk | Lower (known bar) | Higher (new category, unclear rubric) |
| Custom UX | None (Siri‑mediated only) | ≤3 templates, still no custom UI |
| Reusable work | APNs, speakable rendering, workspace picker | **Same** primitives reused |

The clinching point is that **A and B share almost all of the non‑CarPlay work**
— the backend endpoints (§5), the speakable rendering (§3.3), the workspace/auth
model (§8), and APNs (§4) are identical. So "A first, B later" is not throwaway:
Path A de‑risks everything except the CarPlay entitlement/template surface, which
is exactly the part of Path B that is currently unknowable. When Apple publishes
the Path‑B entitlement + template classes and we can exercise them in the
simulator, swapping the thin CarPlay‑scene layer is an incremental follow‑up, not
a rewrite.

**Can we ship A then migrate to B?** Yes, with one caveat: **an app holds one
CarPlay category at a time**, so migration is a *category switch* on the same App
ID (new entitlement grant + new review), not running both simultaneously. Plan
the voice/command layer (§3.2) and the backend (§5) to be independent of which
CarPlay scene drives them, so the migration touches only the native CarPlay/Siri
glue.

### 3.2 Voice‑command → Hypervisor‑API mapping

The driver‑initiated command set maps cleanly onto **existing** endpoints; the
mapping is identical whether a SiriKit intent (Path A) or a Path‑B voice template
invokes it:

| Spoken intent | Meaning | Endpoint(s) | Exists? |
|---|---|---|---|
| "What's my agent doing?" / "status of the deploy chat" | Read status of a/each thread | `GET /api/hypervisor/threads` → `status`; `GET /api/hypervisor/threads/{id}` | ✅ `client.ts:659,692` |
| "Read me the last message" | Speak the latest assistant prose turn | `GET /api/hypervisor/threads/{id}?since=` | ✅ (needs speakable render, §3.3) |
| "Any updates?" / "read my unread" | Which threads have new agent output since I last heard it | **NEW** unread/latest‑activity endpoint | ❌ (§3.4, §5) |
| "Tell it to retry the build" / dictate a reply | Send a follow‑up to a thread | `POST /api/hypervisor/threads/{id}/messages` | ✅ `client.ts:698` |
| "Start a task to run the tests" | New thread with a first message | `POST /api/hypervisor/threads` | ✅ `client.ts:680` |
| "Stop it" | Halt the running turn | `POST /api/hypervisor/threads/{id}/stop` | ✅ `client.ts:713` |
| "Mark as read" | Clear unread after Siri reads it | **NEW** mark‑read (Path A `INSetMessageAttributeIntent`) | ❌ (§3.4, §5) |

`SUGGESTIONS` in `HypervisorScreen.tsx:58-62` ("What's running and how much CPU
am I using?", "Spin up a task to run the tests", "Remember that I deploy with
`make ship`") already model exactly this driver‑friendly phrasing — good seed
utterances for the Siri vocabulary.

### 3.3 The speakable‑rendering gap (most important backend change)

A turn is prose **interleaved with `tool_call` / `tool_result` events**
(`hypervisor_session.py:26-39`), which the phone UI renders as collapsible
"activity chips" (`HypervisorScreen.tsx:834-844`). **These are not speakable.**
Siri (Path A) or the Path‑B voice layer must be handed *clean prose*, not
`tool_call{name:"Bash",input:{...}}`.

We need a **"speakable" projection** of a thread: given the event stream, produce
(a) a one‑line **status summary** ("Running — 2 tools used, last update 40s ago")
and (b) the **latest assistant prose**, with tool activity summarized to a count
("…after running 3 commands") rather than dumped. This is a pure transform over
events we already persist — it belongs next to `build_activity()` /
`transcript()` in `hypervisor_session.py` (`build_activity` at
`hypervisor_session.py:668`; `transcript` at `:1151`) and is exposed via a new
field or endpoint (§5). Keeping it server‑side means Path A's Intents Extension
(which runs *without the app open*) can fetch ready‑to‑speak text with no JS.

### 3.4 The unread gap

`summary()` (`hypervisor_session.py:1118-1130`) has no unread/last‑seen concept.
For "read my unread" and Path A's `INSearchForMessagesIntent`, we need a
**per‑thread last‑agent‑message + a client‑supplied last‑seen `seq`** (or
server‑tracked read cursor). Cheapest design: extend `summary()` with
`last_event_seq` and `last_message` (latest assistant prose), and let the client
track "last heard seq" locally — no server‑side per‑user state, consistent with
the current stateless Bearer model (§8).

### 3.5 Eligibility assessment (does a "workspace assistant" qualify?)

- **Path A:** The Communication category is defined for *messaging* apps. Our
  honest framing is "a messaging app where the other party is your workspace
  agent." This is defensible (Siri already fronts bot/assistant messaging), but
  there is **rejection risk** if a reviewer reads "Communication = human‑to‑human
  only." **Mitigation:** present each thread as a named conversation, implement
  all three messaging intents fully, and keep the CarPlay surface strictly
  voice/notification (no attempt to draw custom UI).
- **Path B:** Purpose‑built for exactly this ("respond to questions/requests"
  voice agent), so conceptually a *better* fit — but the entitlement is
  Apple‑granted per‑app with review, the rubric is new/unpublished, and we'd be an
  early applicant. **Higher uncertainty, potentially higher payoff.**
- **Application‑text risk (both):** a "developer workspace assistant" is a
  narrower audience than a consumer messenger; Apple may question fit/《utility in
  a car》. Frame the value as *hands‑free status + dictated instructions to
  long‑running tasks* (a genuine eyes‑free use case), not "chat with an AI while
  driving."

---

## 4. Deliverable 2 (part) — Notifications: do we need APNs?

Two models:

1. **Driver‑initiated only (no proactive announcements).** The driver asks
   "any updates?" and we answer by polling `GET /api/hypervisor/threads` on
   demand. **No APNs required.** This is the honest MVP and matches today's
   polling transport (`client.ts:10-14`). It is the recommended **Phase 1**.
2. **Proactive "you have a new reply" (iMessage‑like).** For the OS/Siri to
   *announce* a new agent message while the app is backgrounded, the arrival must
   reach the device as a **notification**, which means **remote APNs** — the app
   has none today. This is required for the full Path A metaphor and is **Phase 2**.

### Where the pieces live

- **Client:** add `expo-notifications`; obtain a device push token
  (`getDevicePushTokenAsync` for raw APNs) and register it with the workspace
  ([Expo push docs][expo-push]). New EAS credential: an **APNs key**.
- **Server:** a **push‑token registry** and a **sender**. The workspace backend
  (`server.py`) is the natural home — it already owns the Hypervisor sessions and
  their event append path. When a thread's turn completes (the `status → idle`
  transition already emitted at `hypervisor_session.py:37`), fan out an APNs push
  carrying only sender/thread name (Apple requires **no message body** in the
  CarPlay notification; [guide][guide]). This is **net‑new server code**:
  `POST /api/hypervisor/push/register` (store token per host/token identity) +
  an APNs client invoked from the session's turn‑complete hook.
- **Multi‑workspace wrinkle:** the app can point at *many* workspaces (§8). Each
  workspace backend would push independently, so the token registry is per‑host —
  simplest is "register my APNs token with each workspace I've connected to."

**Recommendation:** Phase 1 ships **without** APNs (driver‑initiated, polling).
Add APNs in Phase 2 only once the voice loop is proven.

---

## 5. Deliverable 2 (part) — new/changed backend endpoints

All additive; nothing existing changes shape.

| Change | Where | Why |
|---|---|---|
| Add `last_event_seq` + `last_message` (latest assistant prose) to thread summary | `hypervisor_session.py:1118-1130` (`summary()`) | Unread detection + "read last message" without pulling the full event stream (§3.4) |
| New **speakable** projection: status one‑liner + tool‑summarized latest prose | new fn near `build_activity()` `hypervisor_session.py:668`; exposed on `GET /api/hypervisor/threads/{id}` (add `?format=speakable`) or a new `/speak` route wired in `server.py` near `:5150` | Siri / voice layer needs clean prose, not tool‑call JSON (§3.3) |
| New cheap **cross‑thread latest‑activity** endpoint: `GET /api/hypervisor/activity/latest` → `[{id,title,status,last_event_seq,last_message_ts}]` | new handler in `server.py` near `:5105`, using `HypervisorSession.list()` `hypervisor_session.py:991` | "Any updates?" and Path A's `INSearchForMessagesIntent` need one cheap call, not N thread fetches |
| **APNs push‑token registry + sender** (Phase 2 only) | new `POST /api/hypervisor/push/register` handler in `server.py`; APNs send hooked to the `status→idle` turn‑complete transition | Proactive announcements (§4) |

No change is required to the event schema or the adapters — the speakable render
is a read‑time transform over `events.jsonl`.

---

## 6. Deliverable 3 — Expo integration plan + EAS/Fastlane impact + effort

### 6.1 Does CarPlay + SiriKit force us out of managed Expo? — Yes, into prebuild.

CarPlay and SiriKit are **native Swift**; none of it can live in JS. Adding them
means moving from **pure managed** to **managed‑with‑prebuild** (Continuous
Native Generation): we keep `app.config.ts` as the source of truth but generate
native `ios/` via `expo prebuild`, and inject the Swift via **config plugins**
([dev.to config‑plugin pattern][devto]). Concretely:

- **CarPlay scene + templates:** use a React Native CarPlay binding. **Note:
  `@g4rb4g3/react-native-carplay` was archived on 4 Feb 2026** and explicitly
  superseded by **`@iternio/react-native-auto-play`** ([g4rb4g3 repo][g4]) — the
  spike should target the successor. It provides CarPlay template bindings and
  needs a config plugin at prebuild; it **requires leaving Expo Go** (dev‑client
  or bare) ([sitepen][sitepen]).
- **SiriKit Messaging intents (Path A):** an **Intents App Extension** with pure
  Swift intent handlers, injected via a config plugin using `withDangerousMod`
  (copy Swift into `ios/`) + `withXcodeProject` (add target/build files), bridged
  to JS via a shared **App Group** ([dev.to][devto]). Intents must compile into
  the extension/main target — they can't be pure JS.
- **Entitlements/plist:** add `com.apple.developer.carplay-communication`,
  `com.apple.developer.siri`, `NSSiriUsageDescription` (and for Path B, the new —
  currently unpublished — voice‑assistant entitlement) via the config plugin /
  `app.config.ts` `ios.entitlements` + `infoPlist` (which already holds ATS keys
  at `app.config.ts:24-34`).
- **APNs (Phase 2):** `expo-notifications` plugin + an APNs key in EAS
  credentials.

### 6.2 EAS impact

- EAS Build runs the prebuild phase, so config plugins + native Swift build fine
  on EAS — no self‑hosted Xcode needed for the build itself.
- **Blocker:** a signed CarPlay build needs a **provisioning profile that
  includes the CarPlay entitlement**, and **Apple must grant that entitlement to
  our App ID first** ([newly][newly]). Until Apple approves, EAS cannot produce a
  signable production build (dev/simulator builds can proceed without it). This is
  a **lead‑time dependency**, not an engineering one.
- `eas.json` (currently plain build profiles + Android submit config at
  `mobile/eas.json`) gains APNs credentials in Phase 2; the iOS submit block
  already defers Apple creds to interactive/EAS prompts.

### 6.3 Fastlane impact

**Minimal.** Fastlane here only pushes **store listing metadata**, not binaries
(`Fastfile:1-4`). CarPlay changes the *build*, which is EAS's job. The only
Fastlane touch is optional: refreshed screenshots/marketing copy mentioning
CarPlay (`fastlane/metadata/**`), and possibly a CarPlay‑specific screenshot set.
No new lanes required.

### 6.4 Effort estimate (rough, MVP = Phase 1, Path A voice loop)

| Workstream | Est. |
|---|---|
| Prebuild migration + dev‑client + config‑plugin scaffolding | 3–5 d |
| CarPlay scene wiring (`@iternio/react-native-auto-play`) | 3–5 d |
| SiriKit Intents App Extension (3 messaging intents) + App‑Group bridge | 5–8 d |
| Backend: speakable render + latest‑activity + summary fields (§5) | 2–3 d |
| Workspace‑selection / auth on head unit (§8) | 2–3 d |
| CarPlay Simulator test harness + Siri phrase tuning (§9) | 2–4 d |
| **Entitlement application + App Store review latency** | **calendar weeks, not eng‑days** |
| **Subtotal (engineering)** | **~4–6 eng‑weeks** |
| Phase 2 (APNs client+server, announcements) | +1–2 eng‑weeks |

Headline: **~5–8 engineer‑weeks** of build, dominated by native iOS, plus
**multi‑week Apple entitlement/review calendar latency** that runs in parallel.

---

## 7. Deliverable 4 — Phased rollout

**Phase 0 — Enablement / de‑risk (no user‑facing CarPlay).**
Prebuild migration; add the successor CarPlay binding + config plugin behind a
dev‑client build; land the additive backend endpoints (§5, speakable render +
latest‑activity + summary fields — these are useful to the phone UI too). **File
the Path A CarPlay entitlement request early** (it's the long pole). Exit
criteria: a dev‑client build boots a trivial CarPlay scene in the simulator.

**Phase 1 — MVP: "read status / dictate a message to the agent" (Path A, no push).**
SiriKit Messaging intents wired to the mapping in §3.2, driver‑initiated only:
"what's my agent doing", "read the last message", "tell it to <X>", "start a task
to <Y>", "stop it". Uses only existing + Phase‑0 endpoints; **no APNs**. Exit:
end‑to‑end voice round‑trip in the CarPlay Simulator against a real workspace.

**Phase 2 — Notifications.**
Add `expo-notifications` + APNs key; server push‑token registry + turn‑complete
sender (§4, §5). The OS announces "new reply from <thread>"; Siri reads on
request; `INSetMessageAttributeIntent` marks read. Exit: a completed turn surfaces
as a CarPlay notification without the app foregrounded.

**Phase 3 — Richer command set + Path B migration.**
Broaden vocabulary (multi‑thread digest, "summarize what happened", targeted
"retry the failing test"). When Apple's iOS‑26.4 voice‑assistant entitlement +
templates are documented and testable, **switch the App ID to the Path B
category** and replace the SiriKit‑Messaging front with the native voice
template, reusing the entire backend + auth + speakable layer.

---

## 8. Deliverable 5 (part) — Auth on the head unit (Q6)

Today the app is **multi‑workspace**: user‑supplied `host` + Bearer `token`,
attached per request (`client.ts:78-114`), stored in `expo-secure-store`
(`app.config.ts:51`). The head‑unit voice session must answer **"which
workspace?"** and do it hands‑free:

- **Default workspace.** Add a "CarPlay / voice default workspace" setting so a
  voice session has an unambiguous target without asking. Most users have one
  active workspace; this covers the common case with zero friction.
- **Disambiguation by name.** If multiple are configured, allow "…on <workspace
  name>" and match against the stored connection labels; otherwise fall back to
  the default. Siri's messaging model naturally scopes by "conversation," which
  maps to thread; workspace is the layer above.
- **Token flow is safe & hands‑free** because it's already stored: the Intents
  Extension / CarPlay scene reads host+token from the **shared App Group**
  (populated by the phone app after normal onboarding — no typing in the car).
  **No new login happens on the head unit.** The Bearer model needs no
  interactive step, which is exactly why it suits an eyes‑free surface.
- **Security notes:** (1) tokens must be shared into the App Group with
  appropriate protection (the app already uses secure‑store for the primary
  copy); (2) CarPlay notifications must carry **no message body** (Apple rule,
  [guide][guide]) — only thread/sender name — which also avoids leaking workspace
  output onto a shared car screen.

---

## 9. Deliverable 5 (part) — Testing (Q7)

- **CarPlay Simulator** ships with Xcode (Xcode → Open Developer Tool → Simulator
  → I/O → External Displays → CarPlay). It exercises templates + the Siri flow
  **without a physical head unit** ([sitepen][sitepen]).
- **We have no native project today**, so testing requires the Phase‑0 prebuild +
  **dev‑client** build (`eas build --profile development`, which `eas.json`
  already defines with `"ios": { "simulator": true }`). Run that dev‑client in the
  iOS Simulator, attach the CarPlay external display, and drive the flow.
- **SiriKit** intents can be unit‑tested in isolation and exercised via "Hey
  Siri, …" against the simulator; the App‑Group bridge is testable with a stub
  workspace.
- **Backend** changes (§5) are covered by the existing `server.py` test suite
  (run via **kc-preflight**) — the speakable render + latest‑activity endpoints
  are pure functions over `events.jsonl` and easy to unit‑test.
- **Path B** cannot be meaningfully tested until Apple ships the documented
  entitlement/templates on an iOS 26.4 simulator — an explicit gating risk (§10).

---

## 10. Deliverable 5 (part) — Open risks

| Risk | Path | Severity | Mitigation |
|---|---|---|---|
| **CarPlay entitlement not granted** (Apple gates per‑App‑ID, with review) | A & B | **High — blocks signed builds** | File early (Phase 0); frame as legit hands‑free use; Path A has a known bar |
| **App Store / entitlement rejection** ("Communication = human‑to‑human", or "AI in car" scrutiny) | A & B | Med–High | Full intent implementation; honest "instruct long‑running tasks" framing; no custom UI |
| **Path B entitlement key + template APIs undocumented** (not in public docs as of this spike) | B | High | Don't commit MVP to B; ship A; revisit when docs/testing exist |
| **iOS 26.4 floor** excludes most of the install base | B | Med | Ship A on shipping iOS first |
| **Leaving managed Expo** (prebuild + native Swift + config plugins) raises build/maintenance cost | A & B | Med | Successor lib `@iternio/react-native-auto-play`; keep plugins thin; CNG keeps `app.config.ts` canonical |
| **CarPlay lib churn** — `@g4rb4g3/react-native-carplay` archived Feb 2026 | A & B | Med | Target the successor; pin versions; budget for old‑architecture caveats |
| **APNs adds server surface + per‑host token registry** (multi‑workspace) | A (Phase 2) | Med | Phase 1 is driver‑initiated, no push; add APNs only once voice loop proven |
| **Speakable rendering quality** (tool‑heavy turns don't summarize well aloud) | A & B | Med | Server‑side projection (§3.3); tune summaries; count‑not‑dump tool activity |
| **Multi‑workspace disambiguation by voice** is error‑prone | A & B | Low–Med | Default workspace + name match (§8) |

---

## 11. Sources

Apple / tooling (with confidence flags from the research pass):

- CarPlay entitlements & categories — <https://developer.apple.com/documentation/carplay/requesting-carplay-entitlements> (Apple SPA; corroborated via the guide PDF + per‑key doc pages). Keys are **hyphenated** (`carplay-communication`), not dotted. [newly]: <https://newly.app/how-to/carplay-entitlement> (one‑category rule; entitlement Apple‑granted).
- [ent-comm]: <https://developer.apple.com/documentation/bundleresources/entitlements/com.apple.developer.carplay-communication>
- [ent-msg]: <https://developer.apple.com/documentation/bundleresources/entitlements/com.apple.developer.carplay-messaging>
- [guide]: CarPlay Developer Guide — <https://developer.apple.com/carplay/> (messaging apps must support the 3 intents; notifications carry sender/name only, no body).
- [forum]: <https://developer.apple.com/forums/thread/664874> (Communication app: entitlements + `INSendMessageIntent`/`INSearchForMessagesIntent` + Intents App Extension).
- [ai]: New iOS 26.4 voice/AI CarPlay category — <https://appleinsider.com/articles/26/02/18/ai-agents-are-coming-to-carplay-but-theyre-not-getting-the-keys> (Feb 9 2026 guide update; ≤3 templates; voice‑launch; no custom UI; iOS 26.4+; no vehicle control). **Entitlement key + template classes: unconfirmed / not yet in public docs.**
- [g4]: `@g4rb4g3/react-native-carplay` — <https://github.com/g4rb4g3/react-native-carplay> (**archived 4 Feb 2026 → superseded by `@iternio/react-native-auto-play`**).
- [sitepen]: CarPlay in React Native + Simulator — <https://www.sitepen.com/blog/add-carplay-to-your-react-native-app>
- [devto]: App Intents / SiriKit in an Expo app via config plugin — <https://dev.to/cross19xx/ios-app-intents-in-an-expo-app-38od> (`withDangerousMod` + `withXcodeProject`; App‑Group bridge; forces prebuild).
- [expo-push]: Expo push notifications setup — <https://docs.expo.dev/push-notifications/push-notifications-setup/> and custom FCM/APNs — <https://docs.expo.dev/push-notifications/sending-notifications-custom/>.

Repo (grounding):

- `mobile/src/api/client.ts` — Hypervisor client (`:640-724`), Bearer transport (`:78-114`), polling rationale (`:10-14`).
- `mobile/src/screens/HypervisorScreen.tsx` — event model (`:1-7`), 2 s poll (`:158`), tool‑activity chips (`:834-844`), driver‑friendly suggestions (`:58-62`).
- `mobile/app.config.ts` — managed Expo, plugin list (`:50-79`), iOS infoPlist/ATS (`:24-34`).
- `mobile/eas.json`, `mobile/fastlane/Fastfile` (`:1-4`) — EAS builds; Fastlane = listing metadata only.
- `charts/workspace/server.py` — Hypervisor handlers (`:5093-5279`).
- `charts/workspace/hypervisor_session.py` — event schema (`:26-39`), `build_activity` (`:668`), `list` (`:991`), `status` (`:1099`), `summary` (`:1118-1130`), `transcript` (`:1151`).
