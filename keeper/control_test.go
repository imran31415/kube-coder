package main

import (
	"encoding/json"
	"net/http"
	"os"
	"strings"
	"testing"
)

// authed is the header a legitimate push from the workspace carries.
func authed() map[string]string {
	return map[string]string{"Authorization": "Bearer " + testControlSecret}
}

func TestControlStoresTriggerIDs(t *testing.T) {
	f, k := newFake(t, 0, 0, FieldManager)
	s := newStep4Server(t, f, k)
	body := []byte(`{"triggers":[
		{"kind":"webhook","id":"gh-pr","provider":"github"},
		{"kind":"cron","id":"nightly"},
		{"kind":"page-watch","id":"docs"}
	]}`)
	rec := doBody(s.ControlHandler(), http.MethodPost, "/api/keeper/triggers", body, authed())
	if rec.Code != http.StatusOK {
		t.Fatalf("push = %d %q; want 200", rec.Code, rec.Body.String())
	}
	var ack map[string]int
	if err := json.Unmarshal(rec.Body.Bytes(), &ack); err != nil {
		t.Fatalf("ack %q: %v", rec.Body.String(), err)
	}
	if ack["triggers"] != 3 {
		t.Fatalf("ack = %v; want 3 triggers", ack)
	}
	if got, ok := s.Store.Lookup("webhook", "gh-pr"); !ok || got.Provider != "github" {
		t.Fatalf("webhook lookup = %+v, %v; want the github webhook", got, ok)
	}
	if _, ok := s.Store.Lookup("page-watch", "docs"); !ok {
		t.Fatal("the page-watch id was not stored")
	}
}

// TestControlPushIsTheWholeSet is the delete path: the workspace re-pushes what
// it still has, and what it dropped must stop being able to wake anything.
func TestControlPushIsTheWholeSet(t *testing.T) {
	f, k := newFake(t, 0, 0, FieldManager)
	s := newStep4Server(t, f, k)
	h := s.ControlHandler()
	first := []byte(`{"triggers":[{"kind":"webhook","id":"keep"},{"kind":"webhook","id":"doomed"}]}`)
	if rec := doBody(h, http.MethodPost, "/api/keeper/triggers", first, authed()); rec.Code != http.StatusOK {
		t.Fatalf("first push = %d", rec.Code)
	}
	second := []byte(`{"triggers":[{"kind":"webhook","id":"keep"}]}`)
	if rec := doBody(h, http.MethodPost, "/api/keeper/triggers", second, authed()); rec.Code != http.StatusOK {
		t.Fatalf("second push = %d", rec.Code)
	}
	if _, ok := s.Store.Lookup("webhook", "doomed"); ok {
		t.Fatal("a deleted webhook is still known, so it could still wake the workspace")
	}
}

func TestControlStoresTokenHash(t *testing.T) {
	f, k := newFake(t, 0, 0, FieldManager)
	s := newStep4Server(t, f, k)
	body := []byte(`{"sha256":"` + hashOf("tok-live") + `"}`)
	rec := doBody(s.ControlHandler(), http.MethodPost, "/api/keeper/token-hash", body, authed())
	if rec.Code != http.StatusOK {
		t.Fatalf("push = %d %q; want 200", rec.Code, rec.Body.String())
	}
	if !s.Store.BearerMatches("tok-live") {
		t.Fatal("the pushed hash does not verify the token it was made from")
	}
}

func TestControlRefusesBadCredentials(t *testing.T) {
	cases := []struct {
		name   string
		header map[string]string
	}{
		{"no Authorization header", nil},
		{"the wrong secret", map[string]string{"Authorization": "Bearer nope"}},
		{"a prefix of the secret", map[string]string{"Authorization": "Bearer " + testControlSecret[:len(testControlSecret)-1]}},
		{"the secret plus a byte", map[string]string{"Authorization": "Bearer " + testControlSecret + "x"}},
		{"an empty bearer", map[string]string{"Authorization": "Bearer "}},
		{"the wrong scheme", map[string]string{"Authorization": "Basic " + testControlSecret}},
		{"the secret with no scheme", map[string]string{"Authorization": testControlSecret}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			f, k := newFake(t, 0, 0, FieldManager)
			s := newStep4Server(t, f, k)
			body := []byte(`{"triggers":[{"kind":"webhook","id":"sneaky"}]}`)
			rec := doBody(s.ControlHandler(), http.MethodPost, "/api/keeper/triggers", body, c.header)
			if rec.Code != http.StatusForbidden {
				t.Fatalf("push = %d; want 403", rec.Code)
			}
			if n := s.Store.TriggerCount(); n != 0 {
				t.Fatalf("a refused push stored %d triggers", n)
			}
		})
	}
}

// TestControlAuthRunsBeforeTheBody pins the ordering: an unauthenticated caller
// must not be able to make the keeper parse anything, so a bad secret with a
// malformed body is 403 and never 400.
func TestControlAuthRunsBeforeTheBody(t *testing.T) {
	f, k := newFake(t, 0, 0, FieldManager)
	s := newStep4Server(t, f, k)
	rec := doBody(s.ControlHandler(), http.MethodPost, "/api/keeper/triggers",
		[]byte(`{not json at all`), map[string]string{"Authorization": "Bearer wrong"})
	if rec.Code != http.StatusForbidden {
		t.Fatalf("= %d %q; want 403, i.e. refused before the body was looked at", rec.Code, rec.Body.String())
	}
}

// TestControlFailsClosedWithoutASecret: a missing or empty secret file must
// refuse every push rather than accept them all.
func TestControlFailsClosedWithoutASecret(t *testing.T) {
	t.Run("no file configured", func(t *testing.T) {
		f, k := newFake(t, 0, 0, FieldManager)
		s := newStep4Server(t, f, k)
		s.ControlSecretFile = ""
		rec := doBody(s.ControlHandler(), http.MethodPost, "/api/keeper/triggers", []byte(`{"triggers":[]}`), authed())
		if rec.Code != http.StatusForbidden {
			t.Fatalf("= %d; want 403", rec.Code)
		}
	})
	t.Run("the file is missing", func(t *testing.T) {
		f, k := newFake(t, 0, 0, FieldManager)
		s := newStep4Server(t, f, k)
		if err := os.Remove(s.ControlSecretFile); err != nil {
			t.Fatal(err)
		}
		rec := doBody(s.ControlHandler(), http.MethodPost, "/api/keeper/triggers", []byte(`{"triggers":[]}`), authed())
		if rec.Code != http.StatusForbidden {
			t.Fatalf("= %d; want 403", rec.Code)
		}
	})
	t.Run("the file is empty", func(t *testing.T) {
		f, k := newFake(t, 0, 0, FieldManager)
		s := newStep4Server(t, f, k)
		if err := os.WriteFile(s.ControlSecretFile, []byte("\n"), 0o600); err != nil {
			t.Fatal(err)
		}
		// An empty secret must not be matched by an empty bearer either.
		rec := doBody(s.ControlHandler(), http.MethodPost, "/api/keeper/triggers", []byte(`{"triggers":[]}`),
			map[string]string{"Authorization": "Bearer "})
		if rec.Code != http.StatusForbidden {
			t.Fatalf("= %d; want 403", rec.Code)
		}
	})
}

func TestControlRejectsRubbishPushes(t *testing.T) {
	cases := []struct {
		name string
		path string
		body string
		want int
	}{
		{"invalid JSON", "/api/keeper/triggers", `{nope`, http.StatusBadRequest},
		{"an unknown kind", "/api/keeper/triggers", `{"triggers":[{"kind":"pagewatch","id":"a"}]}`, http.StatusBadRequest},
		{"a traversal id", "/api/keeper/triggers", `{"triggers":[{"kind":"webhook","id":"../../wake"}]}`, http.StatusBadRequest},
		{"an empty id", "/api/keeper/triggers", `{"triggers":[{"kind":"webhook","id":""}]}`, http.StatusBadRequest},
		{"a short token hash", "/api/keeper/token-hash", `{"sha256":"deadbeef"}`, http.StatusBadRequest},
		{"a non-hex token hash", "/api/keeper/token-hash", `{"sha256":"` + strings.Repeat("z", 64) + `"}`, http.StatusBadRequest},
		{"a missing token hash", "/api/keeper/token-hash", `{}`, http.StatusBadRequest},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			f, k := newFake(t, 0, 0, FieldManager)
			s := newStep4Server(t, f, k)
			rec := doBody(s.ControlHandler(), http.MethodPost, c.path, []byte(c.body), authed())
			if rec.Code != c.want {
				t.Fatalf("= %d %q; want %d", rec.Code, rec.Body.String(), c.want)
			}
			if n := s.Store.TriggerCount(); n != 0 {
				t.Fatalf("a rejected push stored %d triggers", n)
			}
			if s.Store.HasTokenHash() {
				t.Fatal("a rejected push stored a token hash")
			}
		})
	}
}

// TestControlDoesNotEchoTheHash: the reply and the log must not repeat a
// rejected value back. A hash is not the token, but a log full of them is an
// invitation.
func TestControlDoesNotEchoTheHash(t *testing.T) {
	f, k := newFake(t, 0, 0, FieldManager)
	s := newStep4Server(t, f, k)
	buf := captureLog(t)
	const probe = "cafebabecafebabe"
	rec := doBody(s.ControlHandler(), http.MethodPost, "/api/keeper/token-hash",
		[]byte(`{"sha256":"`+probe+`"}`), authed())
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("= %d; want 400", rec.Code)
	}
	if strings.Contains(rec.Body.String(), probe) {
		t.Fatalf("the reply echoed the value: %q", rec.Body.String())
	}
	if strings.Contains(buf.String(), probe) {
		t.Fatalf("the log echoed the value: %q", buf.String())
	}
}

func TestControlRefusesOversizeBodies(t *testing.T) {
	f, k := newFake(t, 0, 0, FieldManager)
	s := newStep4Server(t, f, k)
	big := make([]byte, controlMaxBody+1024)
	for i := range big {
		big[i] = 'a'
	}
	rec := doBody(s.ControlHandler(), http.MethodPost, "/api/keeper/triggers", big, authed())
	if rec.Code != http.StatusRequestEntityTooLarge {
		t.Fatalf("= %d %q; want 413", rec.Code, rec.Body.String())
	}
}

func TestControlRoutes(t *testing.T) {
	cases := []struct {
		name   string
		method string
		path   string
		want   int
	}{
		{"GET triggers", http.MethodGet, "/api/keeper/triggers", http.StatusMethodNotAllowed},
		{"PUT token-hash", http.MethodPut, "/api/keeper/token-hash", http.StatusMethodNotAllowed},
		{"an unknown path", http.MethodPost, "/api/keeper/nope", http.StatusNotFound},
		{"the root", http.MethodPost, "/", http.StatusNotFound},
		{"a trailing slash", http.MethodPost, "/api/keeper/triggers/", http.StatusNotFound},
		{"percent-encoded", http.MethodPost, "/api/keeper/%74riggers", http.StatusNotFound},
		// The browser's /oauth-prefixed routes are not this listener's.
		{"the oauth prefix", http.MethodPost, "/oauth/api/keeper/triggers", http.StatusNotFound},
		// The wake routes must not be reachable on the control port at all.
		{"wake on the control port", http.MethodPost, "/api/keeper/wake", http.StatusNotFound},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			f, k := newFake(t, 0, 0, FieldManager)
			s := newStep4Server(t, f, k)
			rec := doBody(s.ControlHandler(), c.method, c.path, []byte(`{"triggers":[]}`), authed())
			if rec.Code != c.want {
				t.Fatalf("%s %s = %d; want %d", c.method, c.path, rec.Code, c.want)
			}
			if len(f.scales) != 0 {
				t.Fatalf("the control listener scaled the workspace: %+v", f.scales)
			}
		})
	}
}
