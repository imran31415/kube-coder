// Store holds the two things the workspace tells the keeper (#728 step 4), plus
// the wake budget that bounds what a stranger's webhook may cost.
//
// Why any of it is on disk: the workspace can only push while it is awake. If
// the keeper restarted while the workspace slept, an in-memory store would come
// back empty, every webhook would answer 404 for ever, and nothing would wake
// the workspace to make it re-push. So the facts live on the keeper's own
// volume and are read back at boot.
//
// Nothing here is a credential the keeper could spend. The API token arrives
// already hashed, which is enough to check a bearer the phone presents and not
// enough to mint one; a trigger entry is an id and a provider name, never a
// secret.
package main

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"strings"
	"sync"
)

// triggerKinds are the only kinds the keeper accepts. It mirrors
// TriggerRunsManager.KINDS in charts/workspace/server.py; the keeper rejects
// anything else rather than storing a kind it would never route.
var triggerKinds = map[string]bool{"webhook": true, "cron": true, "page-watch": true}

// Trigger is one of the workspace's triggers as the keeper needs to see it:
// enough to route a request and to decide how long a queued one may wait, and
// nothing more. Provider is set for webhooks only — it decides the queue TTL,
// because a Slack or Stripe signature covers a timestamp and expires, while a
// GitHub one covers the body alone and keeps indefinitely.
type Trigger struct {
	Kind     string `json:"kind"`
	ID       string `json:"id"`
	Provider string `json:"provider,omitempty"`
}

// persisted is the whole on-disk blob, written as one file so a reader never
// sees half an update.
type persisted struct {
	TokenSHA256 string               `json:"token_sha256,omitempty"`
	Triggers    map[string][]Trigger `json:"triggers,omitempty"`
	Wakes       []int64              `json:"wakes,omitempty"` // Unix seconds, oldest first
}

// Store is the keeper's durable state. One mutex covers the lot: writes are
// rare (a push, a wake) and reads are small, so splitting the lock would buy
// nothing and cost a class of bug.
type Store struct {
	path string

	mu   sync.RWMutex
	data persisted
}

// NewStore opens or creates the store in dir.
//
// A missing directory is fatal, and meant to be: the chart always mounts the
// volume, so a missing one is an operator misconfiguration, and crashlooping
// names it in the first line of the log. A corrupt file is not fatal — see
// below, it heals itself.
func NewStore(dir string) (*Store, error) {
	if dir == "" {
		return nil, errors.New("store: no directory configured (KEEPER_STATE_DIR)")
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, fmt.Errorf("create store dir: %w", err)
	}
	s := &Store{path: filepath.Join(dir, "state.json")}
	err := s.load()
	if err == nil {
		return s, nil
	}
	// A corrupt store must not stop the keeper. The page is what the ingress
	// needs most, and a logged-in browser visit wakes the workspace without
	// consulting any of this — that path is authenticated by the ingress, not
	// by the token hash. So set the bad file aside, start empty, and say what
	// the consequence is. Every fact here is re-pushed at the workspace's next
	// boot, and such a visit is what causes that boot.
	bad := s.path + ".corrupt"
	log.Printf("store: %v; setting it aside as %s and starting empty. Webhook and app wakes fail safe (404/403) until the workspace boots and re-pushes; a logged-in visit still wakes it.", err, bad)
	if rerr := os.Rename(s.path, bad); rerr != nil {
		log.Printf("store: could not set the bad file aside: %v", rerr)
	}
	s.data = persisted{}
	return s, nil
}

// load reads the blob. A file that is not there is an empty store, not an
// error: that is a keeper's first boot.
func (s *Store) load() error {
	b, err := os.ReadFile(s.path)
	if errors.Is(err, os.ErrNotExist) {
		s.data = persisted{}
		return nil
	}
	if err != nil {
		return fmt.Errorf("read store: %w", err)
	}
	var d persisted
	if err := json.Unmarshal(b, &d); err != nil {
		return fmt.Errorf("decode store %s: %w", s.path, err)
	}
	s.data = d
	return nil
}

// save publishes the blob by rename, so neither a reader nor a crash finds a
// half-written file. os.CreateTemp makes the file 0600, which is the mode this
// wants: the hash is not a secret, but nothing else needs to read it either.
//
// It fsyncs the file and deliberately does not fsync the directory. A directory
// fsync is what makes the rename itself survive a power cut, and it is not
// portable to the Windows dev box; the cost of losing one store write is that
// the workspace re-pushes at its next boot, which it does unconditionally. The
// queue has the opposite trade and syncs properly, because a lost queue entry
// is a lost webhook that nothing re-sends.
//
// Callers hold s.mu.
func (s *Store) save() error {
	b, err := json.Marshal(s.data)
	if err != nil {
		return fmt.Errorf("encode store: %w", err)
	}
	f, err := os.CreateTemp(filepath.Dir(s.path), ".state-*.tmp")
	if err != nil {
		return fmt.Errorf("create temp store: %w", err)
	}
	tmp := f.Name()
	defer func() { _ = os.Remove(tmp) }() // a no-op once the rename has happened
	if _, err := f.Write(b); err != nil {
		_ = f.Close()
		return fmt.Errorf("write temp store: %w", err)
	}
	if err := f.Sync(); err != nil {
		_ = f.Close()
		return fmt.Errorf("sync temp store: %w", err)
	}
	if err := f.Close(); err != nil {
		return fmt.Errorf("close temp store: %w", err)
	}
	if err := os.Rename(tmp, s.path); err != nil {
		return fmt.Errorf("rename store: %w", err)
	}
	return nil
}

// SetTokenHash records the SHA-256 of the workspace's API token, as lowercase
// hex. Re-pushing the same hash is free: the workspace sends it at every boot,
// and rewriting the file for an unchanged value is only a chance to corrupt it.
func (s *Store) SetTokenHash(hexDigest string) error {
	h := strings.ToLower(strings.TrimSpace(hexDigest))
	if len(h) != sha256.Size*2 {
		return fmt.Errorf("token hash: want %d hex characters, got %d", sha256.Size*2, len(h))
	}
	if _, err := hex.DecodeString(h); err != nil {
		return fmt.Errorf("token hash: not hex: %w", err)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.data.TokenSHA256 == h {
		return nil
	}
	s.data.TokenSHA256 = h
	return s.save()
}

// HasTokenHash reports whether the workspace has told the keeper its token yet.
// For the startup log line, so "the app cannot wake it" is visible in the log
// before a user discovers it.
func (s *Store) HasTokenHash() bool {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.data.TokenSHA256 != ""
}

// BearerMatches reports whether token is the workspace's API token, by hash.
//
// False when no hash has arrived: before the workspace has spoken the keeper
// cannot tell a real token from a guess, and a keeper that cannot tell must not
// decide. The comparison runs over two fixed-length hex strings through
// hmac.Equal, so it takes the same time whatever a guess shares with the real
// token — a == here would leak the hash a byte at a time.
func (s *Store) BearerMatches(token string) bool {
	s.mu.RLock()
	want := s.data.TokenSHA256
	s.mu.RUnlock()
	if want == "" || token == "" {
		return false
	}
	sum := sha256.Sum256([]byte(token))
	got := hex.EncodeToString(sum[:])
	return hmac.Equal([]byte(got), []byte(want))
}

// SetTriggers replaces every trigger the keeper knows, of every kind.
//
// The whole set, never a delta. A delete that raced a keeper restart would
// otherwise leave a deleted id behind, and a deleted webhook that still wakes
// the workspace is the bug this design exists to prevent. The workspace reads
// three small directories to build this, so sending all of it every time is
// cheaper than being clever.
func (s *Store) SetTriggers(trigs []Trigger) error {
	byKind := make(map[string][]Trigger, len(triggerKinds))
	for _, t := range trigs {
		if !triggerKinds[t.Kind] {
			return fmt.Errorf("unknown trigger kind %q", t.Kind)
		}
		if !validTriggerID(t.ID) {
			return fmt.Errorf("invalid trigger id %q", t.ID)
		}
		byKind[t.Kind] = append(byKind[t.Kind], Trigger{Kind: t.Kind, ID: t.ID, Provider: t.Provider})
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.data.Triggers = byKind
	return s.save()
}

// Lookup finds a trigger by kind and id.
//
// "Unknown" is the safe answer and the only direction this fails in: the caller
// answers 404 and wakes nothing, which is what a stranger walking webhook URLs
// gets. A workspace holds a handful of triggers, so a scan is the right shape.
func (s *Store) Lookup(kind, id string) (Trigger, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	for _, t := range s.data.Triggers[kind] {
		if t.ID == id {
			return t, true
		}
	}
	return Trigger{}, false
}

// TriggerCount is how many triggers the keeper knows, for the log line.
func (s *Store) TriggerCount() int {
	s.mu.RLock()
	defer s.mu.RUnlock()
	n := 0
	for _, ts := range s.data.Triggers {
		n += len(ts)
	}
	return n
}

// Wakes returns the recorded wake times as Unix seconds, oldest first. The
// policy that reads them lives in limit.go; the store only keeps them.
func (s *Store) Wakes() []int64 {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make([]int64, len(s.data.Wakes))
	copy(out, s.data.Wakes)
	return out
}

// SetWakes replaces the recorded wake times.
func (s *Store) SetWakes(w []int64) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.data.Wakes = append([]int64(nil), w...)
	return s.save()
}

// validTriggerID holds every kind to the loosest of the workspace's own id
// rules, WebhookManager._ID_RE (^[a-zA-Z0-9_-]{1,64}$). Crons and page-watches
// are tighter still, so this accepts every real id and rejects the characters
// that would make an id mean something to a router: no dot, no slash, no
// percent. That is the path-traversal guard, placed where an id first enters
// the keeper rather than at each use.
func validTriggerID(id string) bool {
	if id == "" || len(id) > 64 {
		return false
	}
	for i := 0; i < len(id); i++ {
		c := id[i]
		switch {
		case c >= 'a' && c <= 'z', c >= 'A' && c <= 'Z', c >= '0' && c <= '9', c == '_', c == '-':
		default:
			return false
		}
	}
	return true
}
