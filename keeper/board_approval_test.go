package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

func approvalServer(t *testing.T) (*Server, *BoardApprovals) {
	t.Helper()
	proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "" || r.Header.Get("X-Auth-Request-User") != "" {
			t.Error("untrusted identity forwarded")
		}
		if r.Header.Get("Cookie") != "session=human" {
			w.WriteHeader(401)
			return
		}
		w.Header().Set("X-Auth-Request-User", "reviewer")
		w.WriteHeader(202)
	}))
	t.Cleanup(proxy.Close)
	root := t.TempDir()
	secret := filepath.Join(root, "secret")
	if err := os.WriteFile(secret, []byte("control-secret"), 0600); err != nil {
		t.Fatal(err)
	}
	authority := &BoardApprovals{Dir: filepath.Join(root, "approvals"), AuthURL: proxy.URL, Origin: "https://workspace.test"}
	return &Server{ControlSecretFile: secret, BoardApprovals: authority}, authority
}

func approvalCall(s *Server, action string, body boardDecision, cookie, origin string) *httptest.ResponseRecorder {
	bytes, _ := json.Marshal(body)
	req := httptest.NewRequest("POST", "/api/keeper/board-approvals/"+action, strings.NewReader(string(bytes)))
	req.Header.Set("Authorization", "Bearer control-secret")
	req.Header.Set("Cookie", cookie)
	req.Header.Set("Origin", origin)
	req.Header.Set("X-Auth-Request-User", "forged-admin")
	recorder := httptest.NewRecorder()
	s.ControlHandler().ServeHTTP(recorder, req)
	return recorder
}

func decisionFixture() boardDecision {
	return boardDecision{RequestID: "brq-" + strings.Repeat("a", 32), Revision: strings.Repeat("b", 64),
		DecisionID: "decision-1234567890", Decision: "approve", ExpiresAt: float64(time.Now().Unix() + 800)}
}

func TestBoardApprovalRequiresIndependentHumanSession(t *testing.T) {
	s, _ := approvalServer(t)
	for _, pair := range [][2]string{{"", "https://workspace.test"}, {"session=forged", "https://workspace.test"}, {"session=human", "https://evil.test"}} {
		got := approvalCall(s, "decision", decisionFixture(), pair[0], pair[1])
		if got.Code != 401 && got.Code != 403 {
			t.Fatalf("untrusted decision accepted: %d", got.Code)
		}
	}
	if got := approvalCall(s, "grant", boardDecision{RequestID: decisionFixture().RequestID}, "", ""); got.Code != 403 {
		t.Fatalf("grant without approval: %d", got.Code)
	}
}

func TestBoardApprovalPersistsAndReplaysExactDecision(t *testing.T) {
	s, a := approvalServer(t)
	decision := decisionFixture()
	for i := 0; i < 2; i++ {
		got := approvalCall(s, "decision", decision, "session=human", a.Origin)
		if got.Code != 200 {
			t.Fatalf("approve: %d %s", got.Code, got.Body.String())
		}
	}
	// Recreating the authority proves the grant did not live only in memory.
	s.BoardApprovals = &BoardApprovals{Dir: a.Dir, AuthURL: a.AuthURL, Origin: a.Origin}
	got := approvalCall(s, "grant", boardDecision{RequestID: decision.RequestID}, "", "")
	if got.Code != 200 || !strings.Contains(got.Body.String(), "reviewer") {
		t.Fatalf("grant: %d %s", got.Code, got.Body.String())
	}
	decision.Revision = strings.Repeat("c", 64)
	if got := approvalCall(s, "decision", decision, "session=human", a.Origin); got.Code != 409 {
		t.Fatalf("changed scope: %d", got.Code)
	}
}

func TestBoardApprovalConcurrentReplay(t *testing.T) {
	s, a := approvalServer(t)
	var wg sync.WaitGroup
	for i := 0; i < 12; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if got := approvalCall(s, "decision", decisionFixture(), "session=human", a.Origin); got.Code != 200 {
				t.Errorf("replay: %d", got.Code)
			}
		}()
	}
	wg.Wait()
	files, err := os.ReadDir(a.Dir)
	if err != nil || len(files) != 1 {
		t.Fatalf("one durable decision expected: %d %v", len(files), err)
	}
}

func TestBoardApprovalExpiryCorruptionAndUnavailableConfig(t *testing.T) {
	s, a := approvalServer(t)
	decision := decisionFixture()
	decision.ExpiresAt = float64(time.Now().Unix() - 1)
	if got := approvalCall(s, "decision", decision, "session=human", a.Origin); got.Code != 400 {
		t.Fatalf("expired: %d", got.Code)
	}
	if err := os.MkdirAll(a.Dir, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(a.Dir, decision.RequestID+".json"), []byte("broken"), 0600); err != nil {
		t.Fatal(err)
	}
	if got := approvalCall(s, "grant", boardDecision{RequestID: decision.RequestID}, "", ""); got.Code != 503 {
		t.Fatalf("corrupt: %d", got.Code)
	}
	a.AuthURL = ""
	if got := approvalCall(s, "human", boardDecision{}, "session=human", a.Origin); got.Code != 503 {
		t.Fatalf("unconfigured: %d", got.Code)
	}
}
