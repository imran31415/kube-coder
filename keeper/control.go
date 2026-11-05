// The workspace's way in (#728 step 4).
//
// Two pushes, both workspace -> keeper, both carrying facts and no secrets:
//
//	POST /api/keeper/triggers    the ids of every webhook, cron and page-watch
//	POST /api/keeper/token-hash  the SHA-256 of the workspace's API token
//
// They run on their own listener (:8083) because their peer is different from
// every other listener's. The proxy and the app are reached from the ingress
// controller; this is reached only from the workspace pod, and the chart's
// NetworkPolicy says so. Step 3 split the page and wake listeners for the same
// reason — one port, one class of caller.
//
// The shared secret is not redundant with that NetworkPolicy. A NetworkPolicy
// does nothing at all unless the cluster's CNI implements it (minikube's
// default silently ignores every one), so a control port that mints wake
// authority must not have the CNI as its only boundary.
package main

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"strings"
)

// controlMaxBody caps a push. The largest real one is a trigger list, and a
// workspace with a thousand triggers would still fit several times over; the
// cap is here so a wrong turn can't make the keeper allocate.
const controlMaxBody = 64 << 10

// ControlHandler serves :8083.
func (s *Server) ControlHandler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasPrefix(r.URL.Path, "/api/keeper/board-approvals/") && r.URL.RawPath == "" {
			s.handleBoardApproval(w, r)
			return
		}
		path := r.URL.Path
		if r.URL.RawPath != "" {
			path = "" // percent-encoded: never one of ours
		}
		if path != "/api/keeper/triggers" && path != "/api/keeper/token-hash" {
			writeJSON(w, http.StatusNotFound, map[string]string{"error": "not found"})
			return
		}
		if r.Method != http.MethodPost {
			methodNotAllowed(w, "POST")
			return
		}
		// Authenticate before reading a byte of the body: an unauthenticated
		// caller should not be able to make the keeper parse anything.
		if !s.authControl(r) {
			writeJSON(w, http.StatusForbidden, map[string]string{"error": "forbidden"})
			return
		}
		body, err := readControlBody(w, r)
		if err != nil {
			log.Printf("control: %s: %v", path, err)
			writeJSON(w, http.StatusRequestEntityTooLarge, map[string]string{"error": "body too large"})
			return
		}
		switch path {
		case "/api/keeper/triggers":
			s.handleTriggers(w, body)
		case "/api/keeper/token-hash":
			s.handleTokenHash(w, body)
		}
	})
}

// handleTriggers replaces the keeper's whole idea of which triggers exist.
func (s *Server) handleTriggers(w http.ResponseWriter, body []byte) {
	var in struct {
		Triggers []Trigger `json:"triggers"`
	}
	if err := json.Unmarshal(body, &in); err != nil {
		log.Printf("control: triggers: %v", err)
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid JSON"})
		return
	}
	if s.Store == nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "no store"})
		return
	}
	if err := s.Store.SetTriggers(in.Triggers); err != nil {
		// The message names the offending kind or id, and neither is a secret.
		log.Printf("control: triggers: %v", err)
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	log.Printf("control: now holding %d trigger ids", s.Store.TriggerCount())
	writeJSON(w, http.StatusOK, map[string]int{"triggers": s.Store.TriggerCount()})
}

// handleTokenHash records the hash the app's bearer is checked against.
func (s *Server) handleTokenHash(w http.ResponseWriter, body []byte) {
	var in struct {
		SHA256 string `json:"sha256"`
	}
	if err := json.Unmarshal(body, &in); err != nil {
		log.Printf("control: token-hash: %v", err)
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid JSON"})
		return
	}
	if s.Store == nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "no store"})
		return
	}
	if err := s.Store.SetTokenHash(in.SHA256); err != nil {
		// Deliberately does not echo the value back, even though a hash is not
		// the token: a log that prints hashes invites someone to grind them.
		log.Printf("control: token-hash rejected: %v", err)
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid token hash"})
		return
	}
	log.Printf("control: token hash accepted; the app can wake this workspace")
	writeJSON(w, http.StatusOK, map[string]string{"token_hash": "accepted"})
}

// authControl checks the shared secret.
//
// Both sides are hashed before the comparison so it is constant-time whatever
// their lengths — hmac.Equal on raw strings of different length returns early,
// which would leak the secret's length. Reading the file per request is how
// kube.go treats its service-account token, and for the same reason: a mounted
// Secret can change under a running process.
func (s *Server) authControl(r *http.Request) bool {
	got := bearerToken(r)
	if got == "" {
		return false
	}
	want, err := s.controlSecret()
	if err != nil {
		log.Printf("control: cannot read the shared secret, refusing every push: %v", err)
		return false
	}
	if want == "" {
		log.Printf("control: the shared secret is empty, refusing every push")
		return false
	}
	g := sha256.Sum256([]byte(got))
	x := sha256.Sum256([]byte(want))
	return hmac.Equal(g[:], x[:])
}

func (s *Server) controlSecret() (string, error) {
	if s.ControlSecretFile == "" {
		return "", fmt.Errorf("no secret file configured (KEEPER_CONTROL_SECRET_FILE)")
	}
	b, err := os.ReadFile(s.ControlSecretFile)
	if err != nil {
		return "", fmt.Errorf("read control secret: %w", err)
	}
	return strings.TrimSpace(string(b)), nil
}

// readControlBody reads at most controlMaxBody bytes. MaxBytesReader is what
// turns an oversized body into an error instead of an allocation.
func readControlBody(w http.ResponseWriter, r *http.Request) ([]byte, error) {
	defer func() { _ = r.Body.Close() }()
	b, err := io.ReadAll(http.MaxBytesReader(w, r.Body, controlMaxBody))
	if err != nil {
		return nil, fmt.Errorf("read body: %w", err)
	}
	return b, nil
}
