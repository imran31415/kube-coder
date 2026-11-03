package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

// fakeWorkspace stands in for server.py's trigger receivers and records what
// actually arrived on the wire. Everything the keeper claims about byte
// fidelity is checked against what this saw, not against what the keeper meant
// to send.
type fakeWorkspace struct {
	mu   sync.Mutex
	got  []seenRequest
	code int    // what to answer with, 0 = 202
	body string // the body to answer with
	// hijack makes the workspace accept the request and then drop the
	// connection without answering — the ambiguous outcome.
	hijack bool
}

type seenRequest struct {
	Method           string
	Path             string
	RawQuery         string
	Header           http.Header
	Body             []byte
	ContentLength    int64
	TransferEncoding []string
	Host             string
}

func (ws *fakeWorkspace) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	body, _ := io.ReadAll(r.Body)
	ws.mu.Lock()
	ws.got = append(ws.got, seenRequest{
		Method: r.Method, Path: r.URL.Path, RawQuery: r.URL.RawQuery,
		Header: r.Header.Clone(), Body: body,
		ContentLength: r.ContentLength, TransferEncoding: r.TransferEncoding,
		Host: r.Host,
	})
	code, respBody, hijack := ws.code, ws.body, ws.hijack
	ws.mu.Unlock()
	if hijack {
		c, _, err := w.(http.Hijacker).Hijack()
		if err == nil {
			_ = c.Close()
		}
		return
	}
	if code == 0 {
		code = http.StatusAccepted
	}
	if respBody == "" {
		respBody = `{"ok":true,"task_id":"t-1"}`
	}
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("X-Workspace-Answered", "yes")
	w.WriteHeader(code)
	_, _ = w.Write([]byte(respBody))
}

func (ws *fakeWorkspace) seen() []seenRequest {
	ws.mu.Lock()
	defer ws.mu.Unlock()
	return append([]seenRequest(nil), ws.got...)
}

func (ws *fakeWorkspace) set(fn func(*fakeWorkspace)) {
	ws.mu.Lock()
	defer ws.mu.Unlock()
	fn(ws)
}

// newProxyServer is newStep4Server plus what step 4b needs: the queue, the wake
// budget, and a workspace to forward to. A sibling of the other constructors
// rather than a wider one, so the step-3 tests keep building exactly what they
// built before.
func newProxyServer(t *testing.T, f *fakeAPI, k *Kube) (*Server, *fakeWorkspace) {
	t.Helper()
	s := newAppServer(t, f, k)
	q, err := NewQueue(filepath.Join(t.TempDir(), "queue"), QueueLimits{TTL: time.Hour})
	if err != nil {
		t.Fatal(err)
	}
	s.Queue = q
	s.Limiter = NewWakeLimiter(0, s.Store)
	ws := &fakeWorkspace{}
	srv := httptest.NewServer(ws)
	t.Cleanup(srv.Close)
	s.WorkspaceURL = srv.URL
	// The ids the workspace would have pushed: one of every kind, and a
	// provider on the webhook so the TTL rules have something to read.
	if err := s.Store.SetTriggers([]Trigger{
		{Kind: "webhook", ID: "gh", Provider: "github"},
		{Kind: "webhook", ID: "slk", Provider: "slack"},
		{Kind: "cron", ID: "nightly"},
		{Kind: "page-watch", ID: "docs"},
	}); err != nil {
		t.Fatal(err)
	}
	return s, ws
}

// post sends a signed-looking webhook the way nginx would.
func post(h http.Handler, path string, body []byte, extra map[string]string) *httptest.ResponseRecorder {
	hdr := map[string]string{
		"Content-Type":        "application/json",
		"X-Hub-Signature-256": "sha256=deadbeef",
		"X-Github-Delivery":   "11112222",
		"X-Forwarded-For":     "203.0.113.7",
	}
	for k, v := range extra {
		hdr[k] = v
	}
	return doBody(h, http.MethodPost, path, body, hdr)
}

func codeOf(t *testing.T, rec *httptest.ResponseRecorder) string {
	t.Helper()
	var m map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &m); err != nil {
		t.Fatalf("body %q: %v", rec.Body.String(), err)
	}
	s, _ := m["code"].(string)
	return s
}

// ── A. byte fidelity ────────────────────────────────────────────────────────

func TestProxyForwardsAnAwakeWebhookByteForByte(t *testing.T) {
	f, k := newFake(t, 1, 1, FieldManager)
	s, ws := newProxyServer(t, f, k)
	body := []byte(`{"action":"opened","n":1}`)
	rec := post(s.ProxyHandler(), "/api/webhooks/gh?x=1", body, nil)
	if rec.Code != http.StatusAccepted {
		t.Fatalf("= %d %q; want the workspace's own 202 mirrored", rec.Code, rec.Body.String())
	}
	if rec.Body.String() != `{"ok":true,"task_id":"t-1"}` {
		t.Fatalf("body = %q; the workspace's answer must come back verbatim — GitHub shows it in the delivery log", rec.Body.String())
	}
	if rec.Header().Get("X-Workspace-Answered") != "yes" {
		t.Fatalf("the workspace's response headers were dropped: %v", rec.Header())
	}
	got := ws.seen()
	if len(got) != 1 {
		t.Fatalf("the workspace saw %d requests; want 1", len(got))
	}
	r := got[0]
	if r.Method != http.MethodPost || r.Path != "/api/webhooks/gh" {
		t.Fatalf("arrived as %s %s", r.Method, r.Path)
	}
	if r.RawQuery != "x=1" {
		t.Fatalf("query = %q; a dropped query string would break a Stripe-style URL", r.RawQuery)
	}
	if string(r.Body) != string(body) {
		t.Fatalf("body = %q; want %q — the HMAC covers these exact bytes", r.Body, body)
	}
	if r.Header.Get("X-Hub-Signature-256") != "sha256=deadbeef" {
		t.Fatal("the signature header did not survive the forward")
	}
	if r.Header.Get("X-Forwarded-For") != "203.0.113.7" {
		t.Fatalf("X-Forwarded-For = %q; the ingress already recorded who called and the ledger reads it", r.Header.Get("X-Forwarded-For"))
	}
	if s.Queue.Len() != 0 {
		t.Fatalf("queue holds %d after a delivered forward; want 0", s.Queue.Len())
	}
	if len(f.scales) != 0 {
		t.Fatalf("an awake workspace was scaled: %+v", f.scales)
	}
}

// The trap this whole file's header is about. handle_webhook_receive reads
// exactly int(headers['Content-Length']) (handlers/triggers.py:156) and nothing
// in server.py de-chunks a request, so a chunked forward runs the HMAC over
// b” and answers 404 with ledger reason='bad_signature' — which reads as a
// wrong secret. The forward must therefore always state a definite length.
func TestProxyForwardsADefiniteContentLengthNeverChunked(t *testing.T) {
	for _, body := range [][]byte{{}, []byte("{}"), []byte(strings.Repeat("a", 70000))} {
		f, k := newFake(t, 1, 1, FieldManager)
		s, ws := newProxyServer(t, f, k)
		post(s.ProxyHandler(), "/api/webhooks/gh", body, nil)
		got := ws.seen()
		if len(got) != 1 {
			t.Fatalf("body of %d bytes: the workspace saw %d requests", len(body), len(got))
		}
		r := got[0]
		if len(r.TransferEncoding) != 0 {
			t.Fatalf("body of %d bytes arrived with Transfer-Encoding %v; server.py would HMAC over b''", len(body), r.TransferEncoding)
		}
		if r.ContentLength != int64(len(body)) {
			t.Fatalf("Content-Length = %d for a %d-byte body; server.py reads exactly this number", r.ContentLength, len(body))
		}
		if len(r.Body) != len(body) {
			t.Fatalf("the workspace read %d bytes of a %d-byte body", len(r.Body), len(body))
		}
	}
}

// A body is a byte stream, not text. Anything here that comes out different is
// the keeper treating it as a string somewhere, and every such mutation is a
// silent 404 for its owner.
func TestProxyDoesNotTouchTheBytes(t *testing.T) {
	bodies := map[string][]byte{
		"a NUL":           {'{', 0x00, '}'},
		"CRLF":            []byte("{\r\n}"),
		"a lone CR":       []byte("{\r}"),
		"invalid utf-8":   {0xff, 0xfe, 0x80},
		"a 4-byte emoji":  []byte(`{"m":"💀"}`),
		"all 256 octets":  allOctets(),
		"exactly 1 MiB":   make([]byte, proxyMaxBody),
		"an empty body":   {},
		"a single space":  []byte(" "),
		"not JSON at all": []byte("<xml/>"),
	}
	for name, body := range bodies {
		t.Run(name, func(t *testing.T) {
			f, k := newFake(t, 1, 1, FieldManager)
			s, ws := newProxyServer(t, f, k)
			rec := post(s.ProxyHandler(), "/api/webhooks/gh", body, nil)
			if rec.Code != http.StatusAccepted {
				t.Fatalf("= %d %q", rec.Code, rec.Body.String())
			}
			got := ws.seen()
			if len(got) != 1 || string(got[0].Body) != string(body) {
				t.Fatalf("the %d bytes did not arrive unchanged", len(body))
			}
		})
	}
}

func allOctets() []byte {
	b := make([]byte, 256)
	for i := range b {
		b[i] = byte(i)
	}
	return b
}

// The cap has to agree with the other two opinions in the system: nginx's
// proxy-body-size: 1m and handle_webhook_receive's `> 1*1024*1024`. Exactly
// 1 MiB passes, one byte more is a 413 in server.py's own wording.
func TestProxyCapsTheBodyAtExactlyOneMiB(t *testing.T) {
	f, k := newFake(t, 0, 0, FieldManager)
	s, _ := newProxyServer(t, f, k)
	rec := post(s.ProxyHandler(), "/api/webhooks/gh", make([]byte, proxyMaxBody+1), nil)
	if rec.Code != http.StatusRequestEntityTooLarge {
		t.Fatalf("1 MiB + 1 = %d; want 413", rec.Code)
	}
	if got := errorOf(t, rec); got != "payload too large" {
		t.Fatalf("message = %q; want server.py's own wording", got)
	}
	if s.Queue.Len() != 0 {
		t.Fatalf("an oversized body was queued: %d", s.Queue.Len())
	}
	if len(f.scales) != 0 {
		t.Fatalf("an oversized body woke the workspace: %+v", f.scales)
	}
}

// Go canonicalises header names, and a sender may send the same header twice.
// Both values must go on, because only the workspace gets to decide what two
// signatures mean.
//
// Trailing whitespace is a separate matter and is NOT preserved: HTTP itself
// strips the optional whitespace around a header value, so "sha256=two " comes
// out of any receiving parser as "sha256=two" — measured here through a real
// net/http server. It would do the same travelling from nginx straight to
// server.py, which also calls .strip() before comparing (_verify_generic), so
// nothing the keeper does changes what a signature check sees.
func TestProxyKeepsRepeatedHeaders(t *testing.T) {
	f, k := newFake(t, 1, 1, FieldManager)
	s, ws := newProxyServer(t, f, k)
	req := httptest.NewRequest(http.MethodPost, "/api/webhooks/gh", strings.NewReader("{}"))
	req.Header.Add("X-Hub-Signature-256", "sha256=one")
	req.Header.Add("X-Hub-Signature-256", "sha256=two ") // the space is HTTP's to drop
	req.Header.Add("X-Custom", "a")
	s.ProxyHandler().ServeHTTP(httptest.NewRecorder(), req)
	got := ws.seen()
	if len(got) != 1 {
		t.Fatalf("saw %d requests", len(got))
	}
	sig := got[0].Header["X-Hub-Signature-256"]
	if len(sig) != 2 || sig[0] != "sha256=one" || strings.TrimSpace(sig[1]) != "sha256=two" {
		t.Fatalf("signatures = %q; want both values, in order", sig)
	}
	if got := got[0].Header.Get("X-Custom"); got != "a" {
		t.Fatalf("an unrelated header was dropped: %q", got)
	}
}

// Hop-by-hop headers describe the connection they arrived on, not the request.
// Content-Length is in that list because the forward states its own, from the
// bytes it actually holds.
func TestProxyStripsHopByHopHeaders(t *testing.T) {
	f, k := newFake(t, 1, 1, FieldManager)
	s, ws := newProxyServer(t, f, k)
	post(s.ProxyHandler(), "/api/webhooks/gh", []byte("{}"), map[string]string{
		"Connection": "close",
		"Te":         "trailers",
		"Upgrade":    "websocket",
	})
	h := ws.seen()[0].Header
	for _, name := range []string{"Te", "Upgrade"} {
		if h.Get(name) != "" {
			t.Fatalf("%s was forwarded: %q", name, h.Get(name))
		}
	}
}

// Every status the workspace can answer with comes back as itself. A 409 from
// the replay cache and a 404 from a bad signature mean different things to the
// person reading GitHub's delivery log, and the keeper must not flatten them.
func TestProxyMirrorsEveryStatusVerbatim(t *testing.T) {
	for _, code := range []int{200, 202, 400, 404, 409, 413, 500} {
		f, k := newFake(t, 1, 1, FieldManager)
		s, ws := newProxyServer(t, f, k)
		ws.set(func(w *fakeWorkspace) { w.code = code; w.body = fmt.Sprintf(`{"c":%d}`, code) })
		rec := post(s.ProxyHandler(), "/api/webhooks/gh", []byte("{}"), nil)
		if rec.Code != code {
			t.Fatalf("workspace answered %d, sender saw %d", code, rec.Code)
		}
		if rec.Body.String() != fmt.Sprintf(`{"c":%d}`, code) {
			t.Fatalf("body for %d = %q", code, rec.Body.String())
		}
		if s.Queue.Len() != 0 {
			t.Fatalf("a %d answer left %d entries queued; the workspace has decided", code, s.Queue.Len())
		}
	}
}

// ── B. the trigger-id set is the only thing standing between a stranger and a
// 4 GiB pod ─────────────────────────────────────────────────────────────────

func TestProxyRefusesUnknownIdsWithoutWaking(t *testing.T) {
	paths := []string{
		"/api/webhooks/nope",
		"/api/webhooks/GH",  // the ids are case-sensitive
		"/api/webhooks/gh2", // a near miss
		"/api/webhooks/g",   // a prefix of a real one
		"/api/triggers/cron-fire/nope",
		"/api/triggers/page-watch-check/nope",
		// A real id of the wrong kind: a cron id fired at the webhook path.
		"/api/webhooks/nightly",
		"/api/triggers/cron-fire/gh",
		"/api/triggers/page-watch-check/nightly",
	}
	for _, p := range paths {
		t.Run(p, func(t *testing.T) {
			f, k := newFake(t, 0, 0, FieldManager)
			s, ws := newProxyServer(t, f, k)
			rec := post(s.ProxyHandler(), p, []byte("{}"), nil)
			if rec.Code != http.StatusNotFound {
				t.Fatalf("= %d %q; want 404", rec.Code, rec.Body.String())
			}
			if got := errorOf(t, rec); got != "Not found or unauthorized" {
				t.Fatalf("message = %q; want server.py's own wording, so a probe cannot tell the cases apart", got)
			}
			if len(f.scales) != 0 {
				t.Fatalf("an unknown id woke the workspace: %+v", f.scales)
			}
			if s.Queue.Len() != 0 {
				t.Fatalf("an unknown id was queued: %d entries", s.Queue.Len())
			}
			if len(ws.seen()) != 0 {
				t.Fatal("an unknown id was forwarded to the workspace")
			}
		})
	}
}

// A thousand guesses. Any wake at all here is the headline security bug of this
// feature, and the log must not grow a line per guessed id either — that would
// be a disk-fill primitive on a public URL.
func TestProxyGuessedIdsNeverWakeTheWorkspace(t *testing.T) {
	f, k := newFake(t, 0, 0, FieldManager)
	s, _ := newProxyServer(t, f, k)
	buf := captureLog(t)
	h := s.ProxyHandler()
	for i := 0; i < 1000; i++ {
		rec := post(h, fmt.Sprintf("/api/webhooks/guess%04d", i), []byte("{}"), nil)
		if rec.Code != http.StatusNotFound {
			t.Fatalf("guess %d = %d; want 404", i, rec.Code)
		}
	}
	if len(f.scales) != 0 {
		t.Fatalf("1000 guesses produced %d scale writes", len(f.scales))
	}
	if s.Queue.Len() != 0 {
		t.Fatalf("1000 guesses queued %d entries", s.Queue.Len())
	}
	if n := strings.Count(buf.String(), "\n"); n > 1000 {
		t.Fatalf("%d log lines for 1000 guesses", n)
	}
	if f.gets > 0 {
		t.Fatalf("1000 guesses cost %d Kubernetes API reads; an unknown id must be refused before any lookup", f.gets)
	}
}

// Path traversal, in every shape that reaches a Go handler. The wake endpoint
// lives on this same listener, so a traversal that resolved would be an
// unauthenticated wake.
func TestProxyRefusesTraversalAndEncodedPaths(t *testing.T) {
	paths := []string{
		"/api/webhooks/../../api/keeper/wake",
		"/api/webhooks/..%2f..%2fapi/keeper/wake",
		"/api/webhooks/%2e%2e%2f%2e%2e%2fapi/keeper/wake",
		"/api/webhooks/gh/../../keeper/wake",
		"/api/webhooks/gh%00",
		"/api/webhooks/%67h", // a percent-encoded real id
		"/api/webhooks/gh/extra",
		"/api/webhooks/",
		"/api/webhooks",
		"/api/triggers/cron-fire/",
		"/api/triggers/cron-fire/nightly/../../../api/keeper/wake",
	}
	for _, p := range paths {
		t.Run(p, func(t *testing.T) {
			f, k := newFake(t, 0, 0, FieldManager)
			s, ws := newProxyServer(t, f, k)
			rec := post(s.ProxyHandler(), p, []byte("{}"), nil)
			if rec.Code == http.StatusOK || rec.Code == http.StatusAccepted {
				t.Fatalf("= %d %q; a traversal must not be served", rec.Code, rec.Body.String())
			}
			if len(f.scales) != 0 {
				t.Fatalf("%s woke the workspace: %+v", p, f.scales)
			}
			if len(ws.seen()) != 0 {
				t.Fatalf("%s was forwarded", p)
			}
			if s.Queue.Len() != 0 {
				t.Fatalf("%s was queued", p)
			}
		})
	}
}

// A deleted trigger must stop being able to wake the workspace the moment the
// workspace says so — which is why the push carries the whole set rather than
// deltas.
func TestProxyStopsWakingForADeletedTrigger(t *testing.T) {
	f, k := newFake(t, 0, 0, FieldManager)
	s, _ := newProxyServer(t, f, k)
	if err := s.Store.SetTriggers([]Trigger{{Kind: "cron", ID: "nightly"}}); err != nil {
		t.Fatal(err)
	}
	rec := post(s.ProxyHandler(), "/api/webhooks/gh", []byte("{}"), nil)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("= %d; a webhook deleted from the set must 404", rec.Code)
	}
	if len(f.scales) != 0 {
		t.Fatalf("a deleted webhook woke the workspace: %+v", f.scales)
	}
}

// Only POST. A browser that wandered onto a webhook URL must not start a pod.
func TestProxyRefusesNonPost(t *testing.T) {
	for _, m := range []string{http.MethodGet, http.MethodHead, http.MethodPut, http.MethodDelete} {
		f, k := newFake(t, 0, 0, FieldManager)
		s, _ := newProxyServer(t, f, k)
		rec := do(s.ProxyHandler(), m, "/api/webhooks/gh", nil)
		if rec.Code != http.StatusMethodNotAllowed {
			t.Fatalf("%s = %d; want 405", m, rec.Code)
		}
		if len(f.scales) != 0 {
			t.Fatalf("%s woke the workspace: %+v", m, f.scales)
		}
	}
}

// The app's own routes still live on this listener, and the background chatter
// the phone makes must never start a pod.
func TestProxyKeepsTheAppRoutesAndWakesForNothingElse(t *testing.T) {
	f, k := newFake(t, 0, 0, FieldManager)
	s, _ := newProxyServer(t, f, k)
	h := s.ProxyHandler()
	if rec := do(h, http.MethodGet, "/api/keeper/status", appAuth()); rec.Code != http.StatusOK {
		t.Fatalf("the app's status = %d; the proxy must not shadow it", rec.Code)
	}
	for _, p := range []string{"/health", "/metrics", "/api/push/register", "/api/claude/tasks", "/"} {
		if rec := do(h, http.MethodGet, p, nil); rec.Code != http.StatusNotFound {
			t.Fatalf("%s = %d; want 404 from this listener", p, rec.Code)
		}
	}
	if len(f.scales) != 0 {
		t.Fatalf("the app's background chatter woke the workspace: %+v", f.scales)
	}
}

// ── D. asleep: queue, wake, replay ──────────────────────────────────────────

func TestProxyQueuesAndWakesForAnAsleepWorkspace(t *testing.T) {
	f, k := newFake(t, 0, 0, FieldManager)
	s, ws := newProxyServer(t, f, k)
	body := []byte(`{"action":"opened"}`)
	rec := post(s.ProxyHandler(), "/api/webhooks/gh?a=b", body, nil)
	if rec.Code != http.StatusAccepted {
		t.Fatalf("= %d %q; want 202", rec.Code, rec.Body.String())
	}
	if codeOf(t, rec) != "workspace_waking" {
		t.Fatalf("code = %q", codeOf(t, rec))
	}
	if len(f.scales) != 1 || f.scales[0].Replicas != 1 {
		t.Fatalf("scales = %+v; want one scale to 1", f.scales)
	}
	// The 202 must be on the wire before the wake, not merely written before
	// it: net/http holds a small response until the handler returns, so an
	// unflushed answer followed by a slow wake would have GitHub record a
	// failed delivery for a request that is queued and will run.
	if !rec.Flushed {
		t.Fatal("the 202 was not flushed before the wake")
	}
	if s.Queue.Len() != 1 {
		t.Fatalf("queue holds %d; want the request it accepted", s.Queue.Len())
	}
	if len(ws.seen()) != 0 {
		t.Fatal("the request was forwarded to a workspace that was not there")
	}

	// The workspace comes up and the drain loop delivers it, once, unchanged.
	f.set(func(f *fakeAPI) { f.ready = 1 })
	s.drainOnce(context.Background())
	got := ws.seen()
	if len(got) != 1 {
		t.Fatalf("the woken workspace saw %d replays; want exactly 1", len(got))
	}
	if string(got[0].Body) != string(body) || got[0].RawQuery != "a=b" {
		t.Fatalf("the replay was not byte-identical: %q %q", got[0].Body, got[0].RawQuery)
	}
	if got[0].Header.Get("X-Hub-Signature-256") != "sha256=deadbeef" {
		t.Fatal("the replay lost the signature header")
	}
	if s.Queue.Len() != 0 {
		t.Fatalf("queue holds %d after a delivered replay", s.Queue.Len())
	}
	// And a second drain does not deliver it again.
	s.drainOnce(context.Background())
	if len(ws.seen()) != 1 {
		t.Fatalf("a second drain replayed it again: %d deliveries", len(ws.seen()))
	}
}

// Already starting: the pod is on its way, so spending a wake token on it buys
// nothing — and on a cold start that token is the one a real wake needs.
func TestProxyQueuesWithoutWakingAStartingWorkspace(t *testing.T) {
	f, k := newFake(t, 1, 0, FieldManager) // replicas 1, no ready pod
	s, _ := newProxyServer(t, f, k)
	rec := post(s.ProxyHandler(), "/api/webhooks/gh", []byte("{}"), nil)
	if rec.Code != http.StatusAccepted {
		t.Fatalf("= %d; want 202", rec.Code)
	}
	if len(f.scales) != 0 {
		t.Fatalf("a starting workspace was written to: %+v", f.scales)
	}
	if s.Queue.Len() != 1 {
		t.Fatalf("queue holds %d; want 1", s.Queue.Len())
	}
}

// An admin stop can last days, so a queue that accepted requests for it would
// be a way to fill a volume from the internet.
func TestProxyRefusesAStoppedWorkspaceAndQueuesNothing(t *testing.T) {
	f, k := newFake(t, 0, 0) // replicas 0 owned by nobody
	s, _ := newProxyServer(t, f, k)
	h := s.ProxyHandler()
	for i := 0; i < 50; i++ {
		rec := post(h, "/api/webhooks/gh", []byte("{}"), nil)
		if rec.Code != http.StatusServiceUnavailable {
			t.Fatalf("= %d %q; want 503", rec.Code, rec.Body.String())
		}
		if codeOf(t, rec) != "workspace_stopped" {
			t.Fatalf("code = %q; want workspace_stopped", codeOf(t, rec))
		}
	}
	if s.Queue.Len() != 0 {
		t.Fatalf("a stopped workspace queued %d requests; that is a disk-fill primitive", s.Queue.Len())
	}
	if len(f.scales) != 0 {
		t.Fatalf("a stopped workspace was woken: %+v", f.scales)
	}
}

// The API server is unreachable or the Role is wrong. The request is real and
// the keeper has no idea what to do with it, so it keeps it and wakes nothing.
func TestProxyQueuesWithoutWakingWhenTheApiCannotBeRead(t *testing.T) {
	f, k := newFake(t, 0, 0, FieldManager)
	f.set(func(f *fakeAPI) { f.failGet = http.StatusInternalServerError })
	s, _ := newProxyServer(t, f, k)
	rec := post(s.ProxyHandler(), "/api/webhooks/gh", []byte("{}"), nil)
	if rec.Code != http.StatusAccepted {
		t.Fatalf("= %d %q; want 202 — the request is real and kept", rec.Code, rec.Body.String())
	}
	if s.Queue.Len() != 1 {
		t.Fatalf("queue holds %d; want the request", s.Queue.Len())
	}
	if len(f.scales) != 0 {
		t.Fatalf("a wake was decided on no information: %+v", f.scales)
	}
}

// Fifty senders at once against one asleep workspace: one scale write, fifty
// entries, nothing interleaved. This is the -race case.
func TestProxyFiftyConcurrentWebhooksMakeOneWake(t *testing.T) {
	f, k := newFake(t, 0, 0, FieldManager)
	s, _ := newProxyServer(t, f, k)
	h := s.ProxyHandler()
	var wg sync.WaitGroup
	codes := make([]int, 50)
	for i := 0; i < 50; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			rec := post(h, "/api/webhooks/gh", []byte(fmt.Sprintf(`{"n":%d}`, i)), nil)
			codes[i] = rec.Code
		}(i)
	}
	wg.Wait()
	for i, c := range codes {
		if c != http.StatusAccepted {
			t.Fatalf("sender %d got %d; every one must be accepted", i, c)
		}
	}
	if len(f.scales) != 1 {
		t.Fatalf("50 concurrent webhooks made %d scale writes; want 1", len(f.scales))
	}
	if s.Queue.Len() != 50 {
		t.Fatalf("queue holds %d of 50 accepted requests", s.Queue.Len())
	}
}

// A full queue must not swallow a signed webhook silently: whatever happens has
// to be visible to the sender, which is what makes it recoverable.
func TestProxyRefusesVisiblyWhenTheQueueIsFullAndAway(t *testing.T) {
	f, k := newFake(t, 0, 0, FieldManager)
	s, _ := newProxyServer(t, f, k)
	q, err := NewQueue(filepath.Join(t.TempDir(), "q"), QueueLimits{MaxEntries: 2})
	if err != nil {
		t.Fatal(err)
	}
	s.Queue = q
	h := s.ProxyHandler()
	for i := 0; i < 2; i++ {
		if rec := post(h, "/api/webhooks/gh", []byte("{}"), nil); rec.Code != http.StatusAccepted {
			t.Fatalf("fill %d = %d", i, rec.Code)
		}
	}
	rec := post(h, "/api/webhooks/gh", []byte("{}"), nil)
	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("= %d %q; want a visible refusal", rec.Code, rec.Body.String())
	}
	if codeOf(t, rec) != "queue_full" {
		t.Fatalf("code = %q; want queue_full", codeOf(t, rec))
	}
}

// Awake and the queue is full: the entry is only crash insurance here, because
// the forward happens now and its answer is mirrored back. Refusing a request
// the workspace can handle, because of a backlog it is draining, is the wrong
// trade.
func TestProxyStillForwardsWhenTheQueueIsFullAndAwake(t *testing.T) {
	f, k := newFake(t, 1, 1, FieldManager)
	s, ws := newProxyServer(t, f, k)
	q, err := NewQueue(filepath.Join(t.TempDir(), "q"), QueueLimits{MaxEntries: 1})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := q.Put(entry("webhook", "old")); err != nil {
		t.Fatal(err)
	}
	s.Queue = q
	rec := post(s.ProxyHandler(), "/api/webhooks/gh", []byte(`{"n":1}`), nil)
	if rec.Code != http.StatusAccepted {
		t.Fatalf("= %d %q; want the workspace's mirrored answer", rec.Code, rec.Body.String())
	}
	if len(ws.seen()) != 1 {
		t.Fatalf("the workspace saw %d requests; want the forward to have happened anyway", len(ws.seen()))
	}
}

// Dial refused on a workspace the cache called awake: usually the pod went away
// between the two. Nothing was written, so the state is re-read — bypassing the
// cache that gave the stale answer — and the request re-decided.
func TestProxyRecoversWhenTheWorkspaceVanishedMidForward(t *testing.T) {
	f, k := newFake(t, 1, 1, FieldManager)
	s, _ := newProxyServer(t, f, k)
	// Awake in the cache, and a workspace address that refuses connections.
	if _, err := s.cachedState(context.Background()); err != nil {
		t.Fatal(err)
	}
	s.WorkspaceURL = "http://" + deadAddr(t)
	f.set(func(f *fakeAPI) { f.replicas = int32p(0); f.ready = 0 }) // asleep now
	rec := post(s.ProxyHandler(), "/api/webhooks/gh", []byte("{}"), nil)
	if rec.Code != http.StatusAccepted {
		t.Fatalf("= %d %q; want 202 after re-deciding from fresh state", rec.Code, rec.Body.String())
	}
	if len(f.scales) != 1 {
		t.Fatalf("scales = %+v; the re-read should have found it asleep and woken it", f.scales)
	}
	if s.Queue.Len() != 1 {
		t.Fatalf("queue holds %d; the request must be kept", s.Queue.Len())
	}
}

// The workspace took the request and died before answering. It may have run.
// A webhook is kept for another attempt; a cron is dropped, because
// handle_cron_fire has no dedup and two replays are two agent runs.
func TestProxyAmbiguousForwardAppliesPerKindSemantics(t *testing.T) {
	for _, c := range []struct {
		path string
		kind string
		kept int
	}{
		{"/api/webhooks/gh", "webhook", 1},
		{"/api/triggers/cron-fire/nightly", "cron", 0},
		{"/api/triggers/page-watch-check/docs", "page-watch", 0},
	} {
		t.Run(c.kind, func(t *testing.T) {
			f, k := newFake(t, 1, 1, FieldManager)
			s, ws := newProxyServer(t, f, k)
			ws.set(func(w *fakeWorkspace) { w.hijack = true })
			rec := post(s.ProxyHandler(), c.path, []byte("{}"), nil)
			if rec.Code != http.StatusAccepted {
				t.Fatalf("= %d %q; want 202, the request is accepted and unconfirmed", rec.Code, rec.Body.String())
			}
			if s.Queue.Len() != c.kept {
				t.Fatalf("%s left %d entries; want %d (%s)", c.kind, s.Queue.Len(), c.kept, semanticsFor(c.kind))
			}
		})
	}
}

// A keeper killed between the 202 and the delivery comes back and delivers.
func TestProxyQueueSurvivesAKeeperRestart(t *testing.T) {
	f, k := newFake(t, 0, 0, FieldManager)
	s, ws := newProxyServer(t, f, k)
	dir := filepath.Join(t.TempDir(), "queue")
	q, err := NewQueue(dir, QueueLimits{TTL: time.Hour})
	if err != nil {
		t.Fatal(err)
	}
	s.Queue = q
	if rec := post(s.ProxyHandler(), "/api/webhooks/gh", []byte(`{"survive":true}`), nil); rec.Code != http.StatusAccepted {
		t.Fatalf("= %d", rec.Code)
	}
	// The pod is killed here: a new Queue over the same volume is what comes
	// back, and the workspace is awake by then.
	again, err := NewQueue(dir, QueueLimits{TTL: time.Hour})
	if err != nil {
		t.Fatal(err)
	}
	s.Queue = again
	f.set(func(f *fakeAPI) { f.ready = 1 })
	s.drainOnce(context.Background())
	got := ws.seen()
	if len(got) != 1 || string(got[0].Body) != `{"survive":true}` {
		t.Fatalf("after a restart the workspace saw %d replays: %+v", len(got), got)
	}
}

// Order is what a sender expects. Three deliveries queued, three replayed in
// the order they arrived.
func TestProxyReplaysInArrivalOrder(t *testing.T) {
	f, k := newFake(t, 0, 0, FieldManager)
	s, ws := newProxyServer(t, f, k)
	h := s.ProxyHandler()
	for i := 0; i < 3; i++ {
		post(h, "/api/webhooks/gh", []byte(fmt.Sprintf(`{"n":%d}`, i)), nil)
	}
	f.set(func(f *fakeAPI) { f.ready = 1 })
	s.drainOnce(context.Background())
	got := ws.seen()
	if len(got) != 3 {
		t.Fatalf("replayed %d of 3", len(got))
	}
	for i, r := range got {
		if want := fmt.Sprintf(`{"n":%d}`, i); string(r.Body) != want {
			t.Fatalf("replay %d = %s; want %s", i, r.Body, want)
		}
	}
}

// A Slack webhook queued past its signing window is expired with a line that
// says so, instead of being replayed into a 404 that reads as a wrong secret.
func TestProxyExpiresASlackWebhookRatherThanReplayingIt(t *testing.T) {
	f, k := newFake(t, 0, 0, FieldManager)
	s, ws := newProxyServer(t, f, k)
	if rec := post(s.ProxyHandler(), "/api/webhooks/slk", []byte("{}"), nil); rec.Code != http.StatusAccepted {
		t.Fatalf("= %d", rec.Code)
	}
	// Age it past the signing window the way a long wake would.
	e, tok, ok := s.Queue.Claim()
	if !ok {
		t.Fatal("claim")
	}
	e.QueuedAt = time.Now().Add(-6 * time.Minute)
	s.Queue.Return(tok, e)

	f.set(func(f *fakeAPI) { f.ready = 1 })
	buf := captureLog(t)
	s.drainOnce(context.Background())
	if len(ws.seen()) != 0 {
		t.Fatal("an expired slack webhook was replayed into a misleading bad_signature")
	}
	if s.Queue.Len() != 0 {
		t.Fatalf("queue holds %d after expiry", s.Queue.Len())
	}
	if !strings.Contains(buf.String(), "slack") {
		t.Fatalf("the expiry was silent: %q", buf.String())
	}
}

// ── E. the wake budget ──────────────────────────────────────────────────────

// Decision 4: an exhausted budget queues and answers, and does not wake. The
// request is not lost — it drains at the next wake from any cause.
func TestProxyQueuesWithoutWakingWhenTheBudgetIsSpent(t *testing.T) {
	f, k := newFake(t, 0, 0, FieldManager)
	s, _ := newProxyServer(t, f, k)
	s.Limiter = NewWakeLimiter(1, s.Store)
	// No state cache for this one. The two webhooks below land in the same
	// millisecond, and with the usual one-second cache the second would be told
	// "starting" — left over from the first one's wake — and skip the budget
	// check entirely. The assertion would then pass without the budget ever
	// being consulted, which is exactly the vacuous test this suite is supposed
	// not to contain. In production a second webhook arrives whole seconds
	// later and reads the state fresh, so this is the realistic path.
	s.cacheFor = 0
	h := s.ProxyHandler()

	if rec := post(h, "/api/webhooks/gh", []byte(`{"n":0}`), nil); rec.Code != http.StatusAccepted {
		t.Fatalf("first = %d", rec.Code)
	}
	if len(f.scales) != 1 {
		t.Fatalf("the first webhook should have woken it: %+v", f.scales)
	}
	// It went back to sleep, and the budget is spent.
	f.set(func(f *fakeAPI) { f.replicas, f.ready, f.owners = int32p(0), 0, []string{FieldManager} })
	rec := post(h, "/api/webhooks/gh", []byte(`{"n":1}`), nil)
	if rec.Code != http.StatusAccepted {
		t.Fatalf("= %d %q; a spent budget must still accept the request", rec.Code, rec.Body.String())
	}
	if len(f.scales) != 1 {
		t.Fatalf("scales = %+v; the spent budget must not have woken it again", f.scales)
	}
	if s.Queue.Len() != 2 {
		t.Fatalf("queue holds %d; both requests must be kept", s.Queue.Len())
	}

	// And it drains once the budget frees up, with no help from anybody.
	s.Limiter.Now = func() time.Time { return time.Now().Add(2 * time.Hour) }
	s.drainOnce(context.Background())
	if len(f.scales) != 2 {
		t.Fatalf("scales = %+v; the drain loop should wake it once the budget frees", f.scales)
	}
}

func TestProxyBudgetDoesNotApplyToPeople(t *testing.T) {
	f, k := newFake(t, 0, 0, FieldManager)
	s, _ := newProxyServer(t, f, k)
	s.Limiter = NewWakeLimiter(1, s.Store)
	// Spend the budget on a webhook.
	post(s.ProxyHandler(), "/api/webhooks/gh", []byte("{}"), nil)
	f.set(func(f *fakeAPI) { f.replicas, f.ready, f.owners = int32p(0), 0, []string{FieldManager} })
	// The app's Wake button must still work: a person holding this workspace's
	// own token is not who the budget protects against, and a button that
	// silently did nothing is a worse failure than an extra pod start.
	rec := do(s.ProxyHandler(), http.MethodPost, "/api/keeper/wake", appAuth())
	if rec.Code != http.StatusOK {
		t.Fatalf("the app's wake = %d %q; the budget must not apply to it", rec.Code, rec.Body.String())
	}
	if len(f.scales) != 2 {
		t.Fatalf("scales = %+v; want the app's wake to have happened", f.scales)
	}
}

// ── the holding page's copy ─────────────────────────────────────────────────

// The status JSON carries why the workspace is starting, so the page can say
// "waking up to run a scheduled task" instead of leaving someone to wonder why
// their workspace started on its own.
func TestStatusCarriesTheWakeReason(t *testing.T) {
	cases := []struct {
		name string
		do   func(*testing.T, *Server)
		want string
	}{
		{"a webhook", func(t *testing.T, s *Server) {
			post(s.ProxyHandler(), "/api/webhooks/gh", []byte("{}"), nil)
		}, reasonWebhook},
		{"a cron", func(t *testing.T, s *Server) {
			post(s.ProxyHandler(), "/api/triggers/cron-fire/nightly", []byte("{}"), nil)
		}, reasonCron},
		{"a page-watch", func(t *testing.T, s *Server) {
			post(s.ProxyHandler(), "/api/triggers/page-watch-check/docs", []byte("{}"), nil)
		}, reasonCron},
		{"the app", func(t *testing.T, s *Server) {
			do(s.ProxyHandler(), http.MethodPost, "/api/keeper/wake", appAuth())
		}, reasonApp},
		{"a browser visit", func(t *testing.T, s *Server) {
			do(s.WakeHandler(), http.MethodPost, "/api/keeper/wake", map[string]string{"X-Keeper-Wake": "1"})
		}, reasonVisit},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			f, k := newFake(t, 0, 0, FieldManager)
			s, _ := newProxyServer(t, f, k)
			c.do(t, s)
			rec := do(s.WakeHandler(), http.MethodGet, "/api/keeper/status", nil)
			got := decode(t, rec)
			if !got.Woken {
				t.Fatalf("state = %+v; want woken", got)
			}
			if got.Reason != c.want {
				t.Fatalf("reason = %q; want %q", got.Reason, c.want)
			}
		})
	}
}

// No reason without a wake, so the page never explains something that did not
// happen — a Recreate restart is not a wake.
func TestStatusHasNoReasonWithoutAWake(t *testing.T) {
	f, k := newFake(t, 1, 0, FieldManager) // starting, nobody woke it
	s, _ := newProxyServer(t, f, k)
	got := decode(t, do(s.WakeHandler(), http.MethodGet, "/api/keeper/status", nil))
	if got.Woken || got.Reason != "" {
		t.Fatalf("= %+v; want no woken and no reason", got)
	}
}

// ── plumbing ────────────────────────────────────────────────────────────────

func TestProxyRouteParsing(t *testing.T) {
	cases := []struct{ path, kind, id string }{
		{"/api/webhooks/gh", "webhook", "gh"},
		{"/api/triggers/cron-fire/nightly", "cron", "nightly"},
		{"/api/triggers/page-watch-check/docs", "page-watch", "docs"},
		{"/api/webhooks/a/b", "", ""},
		{"/api/keeper/wake", "", ""},
		{"/api/triggers/cron-fire", "", ""},
		{"/health", "", ""},
	}
	for _, c := range cases {
		kind, id, ok := proxyRoute(c.path)
		if (c.kind == "") == ok {
			t.Fatalf("%s: ok = %v", c.path, ok)
		}
		if ok && (kind != c.kind || id != c.id) {
			t.Fatalf("%s = %s/%s; want %s/%s", c.path, kind, id, c.kind, c.id)
		}
	}
}

func TestEnvIntFallsBackLoudly(t *testing.T) {
	t.Setenv("KEEPER_TEST_N", "nonsense")
	buf := captureLog(t)
	if got := envInt("KEEPER_TEST_N", 7); got != 7 {
		t.Fatalf("= %d; want the default", got)
	}
	if buf.String() == "" {
		t.Fatal("a bad value must be reported, not silently ignored")
	}
	t.Setenv("KEEPER_TEST_N", "12")
	if got := envInt("KEEPER_TEST_N", 7); got != 12 {
		t.Fatalf("= %d; want 12", got)
	}
	t.Setenv("KEEPER_TEST_N", "")
	if got := envInt("KEEPER_TEST_N", 7); got != 7 {
		t.Fatalf("empty = %d; want the default", got)
	}
}

// The forward address, when the chart has not overridden it, is the in-cluster
// Service of the Deployment this keeper looks after — the same address
// CronManager._apply_k8s computes for itself.
func TestWorkspaceURLDefaultsToTheClusterService(t *testing.T) {
	s := &Server{Kube: &Kube{Namespace: "ws-ns", Name: "ws-alice"}}
	if got := s.workspaceURL(); got != "http://ws-alice.ws-ns.svc.cluster.local:6080" {
		t.Fatalf("= %q", got)
	}
	s.WorkspaceURL = "http://keeper-test:6080/"
	if got := s.workspaceURL(); got != "http://keeper-test:6080" {
		t.Fatalf("= %q; a trailing slash would double up on the path", got)
	}
}

// deadAddr is an address nothing listens on: a port that was bound and released.
func deadAddr(t *testing.T) string {
	t.Helper()
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := l.Addr().String()
	_ = l.Close()
	return addr
}

func int32p(n int32) *int32 { return &n }

// An admin stop found on the re-read after a failed dial is answered the way
// the front door answers it, and the entry is forgotten. One rule for a stopped
// workspace, wherever the keeper finds out about it — otherwise a request that
// slipped in a moment before the stop sits on the volume until its TTL.
func TestProxyDropsTheEntryWhenTheStopHappensMidForward(t *testing.T) {
	f, k := newFake(t, 1, 1, FieldManager)
	s, _ := newProxyServer(t, f, k)
	if _, err := s.cachedState(context.Background()); err != nil {
		t.Fatal(err)
	}
	s.WorkspaceURL = "http://" + deadAddr(t)
	// An admin stop: replicas 0, owned by nobody.
	f.set(func(f *fakeAPI) { f.replicas, f.ready, f.owners = int32p(0), 0, nil })
	rec := post(s.ProxyHandler(), "/api/webhooks/gh", []byte("{}"), nil)
	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("= %d %q; want 503", rec.Code, rec.Body.String())
	}
	if codeOf(t, rec) != "workspace_stopped" {
		t.Fatalf("code = %q; want workspace_stopped", codeOf(t, rec))
	}
	if s.Queue.Len() != 0 {
		t.Fatalf("queue holds %d for a stopped workspace; want nothing kept", s.Queue.Len())
	}
	if len(f.scales) != 0 {
		t.Fatalf("a stopped workspace was woken: %+v", f.scales)
	}
}

// The drain loop's own wake must name what is actually waiting. A queue holding
// only webhooks that woke the workspace "to run a scheduled task" would be the
// page confidently telling someone the wrong thing.
func TestDrainWakeNamesWhatIsWaiting(t *testing.T) {
	for _, c := range []struct{ path, want string }{
		{"/api/webhooks/gh", reasonWebhook},
		{"/api/triggers/cron-fire/nightly", reasonCron},
	} {
		t.Run(c.want, func(t *testing.T) {
			f, k := newFake(t, 0, 0, FieldManager)
			s, _ := newProxyServer(t, f, k)
			s.Limiter = NewWakeLimiter(1, s.Store)
			s.cacheFor = 0
			// Spend the budget so the arrival queues without waking, leaving
			// the drain loop to be the one that wakes it.
			if !s.Limiter.Allow() {
				t.Fatal("budget")
			}
			if rec := post(s.ProxyHandler(), c.path, []byte("{}"), nil); rec.Code != http.StatusAccepted {
				t.Fatalf("= %d", rec.Code)
			}
			if len(f.scales) != 0 {
				t.Fatalf("the spent budget woke it anyway: %+v", f.scales)
			}
			s.Limiter.Now = func() time.Time { return time.Now().Add(2 * time.Hour) }
			s.drainOnce(context.Background())
			if len(f.scales) != 1 {
				t.Fatalf("the drain loop did not wake it: %+v", f.scales)
			}
			got := decode(t, do(s.WakeHandler(), http.MethodGet, "/api/keeper/status", nil))
			if got.Reason != c.want {
				t.Fatalf("reason = %q; want %q", got.Reason, c.want)
			}
		})
	}
}
