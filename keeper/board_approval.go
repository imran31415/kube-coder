// Human Board decisions live on the keeper PVC, never in agent-writable storage.
// Even on a CNI that ignores NetworkPolicy, a caller must present a browser
// cookie that the configured OAuth proxy independently validates.
package main

import (
	"encoding/json"
	"errors"
	"io"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"time"
)

// A loopback-only entry point for local integration/live tests, using the same
// auth and durable decisions as the deployed keeper; no Kubernetes client.
func serveLocalBoardApprovals() error {
	dir := os.Getenv("KEEPER_STATE_DIR")
	a := &BoardApprovals{Dir: dir, AuthURL: os.Getenv("KEEPER_BOARD_AUTH_URL"), Origin: os.Getenv("KEEPER_BOARD_ORIGIN")}
	secret := os.Getenv("KEEPER_CONTROL_SECRET_FILE")
	if dir == "" || a.AuthURL == "" || a.Origin == "" || secret == "" {
		return errors.New("local Board approval requires state directory, OAuth auth URL, origin and control-secret file")
	}
	port := os.Getenv("KEEPER_BOARD_PORT")
	if port == "" {
		port = "7072"
	}
	if !regexp.MustCompile(`^[0-9]{1,5}$`).MatchString(port) {
		return errors.New("invalid loopback port")
	}
	s := &Server{ControlSecretFile: secret, BoardApprovals: a}
	server := &http.Server{Addr: "127.0.0.1:" + port, Handler: s.ControlHandler(),
		ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 15 * time.Second, WriteTimeout: 15 * time.Second, IdleTimeout: 30 * time.Second}
	return server.ListenAndServe()
}

type BoardApprovals struct {
	Dir, AuthURL, Origin string
	mu                   sync.Mutex
}

type boardDecision struct {
	RequestID  string  `json:"request_id"`
	Revision   string  `json:"revision"`
	DecisionID string  `json:"decision_id"`
	Decision   string  `json:"decision"`
	ExpiresAt  float64 `json:"expires_at"`
	Actor      string  `json:"actor"`
}

var boardRequestID = regexp.MustCompile(`^brq-[a-f0-9]{32}$`)
var boardRevision = regexp.MustCompile(`^[a-f0-9]{64}$`)
var boardDecisionID = regexp.MustCompile(`^[a-zA-Z0-9-]{16,80}$`)

func (a *BoardApprovals) human(r *http.Request) (string, int) {
	if a == nil || a.AuthURL == "" || a.Origin == "" {
		return "", http.StatusServiceUnavailable
	}
	if r.Header.Get("Origin") != a.Origin || r.Header.Get("Cookie") == "" {
		return "", http.StatusForbidden
	}
	req, err := http.NewRequestWithContext(r.Context(), http.MethodGet, a.AuthURL, nil)
	if err != nil {
		return "", http.StatusServiceUnavailable
	}
	// Never forward claimed identity, access-token or Authorization headers.
	req.Header.Set("Cookie", r.Header.Get("Cookie"))
	client := &http.Client{Timeout: 5 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	resp, err := client.Do(req)
	if err != nil {
		return "", http.StatusServiceUnavailable
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusAccepted && resp.StatusCode != http.StatusOK {
		return "", http.StatusUnauthorized
	}
	actor := resp.Header.Get("X-Auth-Request-User")
	if actor == "" {
		return "", http.StatusUnauthorized
	}
	return actor, http.StatusOK
}

func (s *Server) handleBoardApproval(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		methodNotAllowed(w, "POST")
		return
	}
	if !s.authControl(r) {
		writeJSON(w, 403, map[string]string{"error": "forbidden"})
		return
	}
	a := s.BoardApprovals
	if a == nil || a.Dir == "" {
		writeJSON(w, 503, map[string]string{"error": "approval not configured"})
		return
	}
	action := strings.TrimPrefix(r.URL.Path, "/api/keeper/board-approvals/")
	if action != "human" && action != "decision" && action != "grant" {
		writeJSON(w, 404, map[string]string{"error": "not found"})
		return
	}
	actor := ""
	if action != "grant" {
		var status int
		actor, status = a.human(r)
		if status != 200 {
			writeJSON(w, status, map[string]string{"error": "human session required"})
			return
		}
	}
	if action == "human" {
		writeJSON(w, 200, map[string]string{"actor": actor})
		return
	}
	var in boardDecision
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4096))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&in); err != nil || !boardRequestID.MatchString(in.RequestID) {
		writeJSON(w, 400, map[string]string{"error": "invalid request"})
		return
	}
	if decoder.Decode(&struct{}{}) != io.EOF {
		writeJSON(w, 400, map[string]string{"error": "invalid JSON"})
		return
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	path := filepath.Join(a.Dir, in.RequestID+".json")
	bytes, err := os.ReadFile(path)
	if err == nil {
		var prior boardDecision
		if json.Unmarshal(bytes, &prior) != nil {
			writeJSON(w, 503, map[string]string{"error": "approval record unavailable"})
			return
		}
		if action == "grant" || (prior.Revision == in.Revision && prior.DecisionID == in.DecisionID && prior.Decision == in.Decision && prior.Actor == actor) {
			writeJSON(w, 200, prior)
			return
		}
		writeJSON(w, 409, map[string]string{"error": "decision conflict"})
		return
	}
	if !os.IsNotExist(err) {
		writeJSON(w, 503, map[string]string{"error": "approval storage unavailable"})
		return
	}
	if action == "grant" {
		writeJSON(w, 403, map[string]string{"error": "approval required"})
		return
	}
	now := float64(time.Now().Unix())
	if !boardRevision.MatchString(in.Revision) || !boardDecisionID.MatchString(in.DecisionID) ||
		(in.Decision != "approve" && in.Decision != "reject") || in.ExpiresAt <= now || in.ExpiresAt > now+901 {
		writeJSON(w, 400, map[string]string{"error": "invalid or expired decision"})
		return
	}
	in.Actor = actor
	if err := a.save(path, in); err != nil {
		log.Printf("board approval persist request_id=%s: %v", in.RequestID, err)
		writeJSON(w, 503, map[string]string{"error": "approval storage unavailable"})
		return
	}
	log.Printf("board approval request_id=%s decision=%s actor=%s", in.RequestID, in.Decision, actor)
	writeJSON(w, 200, in)
}

func (a *BoardApprovals) save(path string, decision boardDecision) error {
	if err := os.MkdirAll(a.Dir, 0700); err != nil {
		return err
	}
	f, err := os.CreateTemp(a.Dir, ".approval-")
	if err != nil {
		return err
	}
	defer os.Remove(f.Name())
	if err = json.NewEncoder(f).Encode(decision); err == nil {
		err = f.Sync()
	}
	closeErr := f.Close()
	if err != nil {
		return err
	}
	if closeErr != nil {
		return closeErr
	}
	if err = os.Rename(f.Name(), path); err != nil {
		return err
	}
	dir, err := os.Open(a.Dir)
	if err != nil {
		return err
	}
	defer dir.Close()
	return dir.Sync()
}
