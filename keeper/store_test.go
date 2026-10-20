package main

import (
	"crypto/sha256"
	"encoding/hex"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// hashOf is what the workspace sends: hashlib.sha256(token).hexdigest().
func hashOf(token string) string {
	sum := sha256.Sum256([]byte(token))
	return hex.EncodeToString(sum[:])
}

func newStore(t *testing.T) (*Store, string) {
	t.Helper()
	dir := t.TempDir()
	s, err := NewStore(dir)
	if err != nil {
		t.Fatalf("NewStore: %v", err)
	}
	return s, dir
}

func TestStoreFirstBootIsEmpty(t *testing.T) {
	s, _ := newStore(t)
	if s.HasTokenHash() {
		t.Fatal("a fresh store must not claim a token hash")
	}
	if n := s.TriggerCount(); n != 0 {
		t.Fatalf("trigger count = %d; want 0", n)
	}
	if _, ok := s.Lookup("webhook", "anything"); ok {
		t.Fatal("a fresh store must know no triggers")
	}
}

func TestStoreSurvivesReopen(t *testing.T) {
	s, dir := newStore(t)
	if err := s.SetTokenHash(hashOf("tok-abc")); err != nil {
		t.Fatalf("SetTokenHash: %v", err)
	}
	if err := s.SetTriggers([]Trigger{
		{Kind: "webhook", ID: "gh-pr", Provider: "github"},
		{Kind: "cron", ID: "nightly"},
	}); err != nil {
		t.Fatalf("SetTriggers: %v", err)
	}

	// The case the whole file exists for: the keeper restarted while the
	// workspace slept, so nothing can re-push until something wakes it.
	reopened, err := NewStore(dir)
	if err != nil {
		t.Fatalf("reopen: %v", err)
	}
	if !reopened.BearerMatches("tok-abc") {
		t.Fatal("the token hash did not survive a restart, so the app could never wake a sleeping workspace")
	}
	got, ok := reopened.Lookup("webhook", "gh-pr")
	if !ok || got.Provider != "github" {
		t.Fatalf("Lookup after restart = %+v, %v; want the github webhook", got, ok)
	}
	if _, ok := reopened.Lookup("cron", "nightly"); !ok {
		t.Fatal("the cron id did not survive a restart")
	}
}

func TestStoreCorruptFileStartsEmptyAndStandsAside(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "state.json")
	if err := os.WriteFile(path, []byte(`{"token_sha256": "trunc`), 0o600); err != nil {
		t.Fatalf("seed: %v", err)
	}
	// Not an error and not a panic: the page matters more than the store, and a
	// logged-in visit still wakes the workspace, which makes it re-push.
	s, err := NewStore(dir)
	if err != nil {
		t.Fatalf("a corrupt store must not stop the keeper, got: %v", err)
	}
	if s.HasTokenHash() {
		t.Fatal("a corrupt store must not be believed")
	}
	if _, err := os.Stat(path + ".corrupt"); err != nil {
		t.Fatalf("the bad file should have been set aside: %v", err)
	}
	// And it must still be writable afterwards, so the re-push lands.
	if err := s.SetTokenHash(hashOf("tok-new")); err != nil {
		t.Fatalf("store unusable after recovery: %v", err)
	}
	if !s.BearerMatches("tok-new") {
		t.Fatal("the re-pushed hash did not take")
	}
}

func TestStoreFileIsNotWorldReadable(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("POSIX permission bits are not meaningful here; the keeper only runs on Linux")
	}
	s, dir := newStore(t)
	if err := s.SetTokenHash(hashOf("tok")); err != nil {
		t.Fatalf("SetTokenHash: %v", err)
	}
	fi, err := os.Stat(filepath.Join(dir, "state.json"))
	if err != nil {
		t.Fatalf("stat: %v", err)
	}
	if perm := fi.Mode().Perm(); perm != 0o600 {
		t.Fatalf("mode = %#o; want 0600", perm)
	}
}

func TestBearerMatches(t *testing.T) {
	const real = "tok-correct-horse"
	cases := []struct {
		name    string
		hash    string // "" = never pushed
		present string
		want    bool
	}{
		{"the real token", hashOf(real), real, true},
		{"a wrong token", hashOf(real), "tok-wrong", false},
		{"an empty token", hashOf(real), "", false},
		// The shared-prefix probe. It cannot prove constant time, but it does
		// prove the comparison is not a prefix match.
		{"a long shared prefix", hashOf(real), "tok-correct-hors", false},
		{"one byte longer", hashOf(real), real + "x", false},
		// Before the workspace has spoken the keeper cannot tell a real token
		// from a guess, so it must refuse even the real one.
		{"no hash pushed yet", "", real, false},
		{"no hash pushed, empty token", "", "", false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			s, _ := newStore(t)
			if c.hash != "" {
				if err := s.SetTokenHash(c.hash); err != nil {
					t.Fatalf("SetTokenHash: %v", err)
				}
			}
			if got := s.BearerMatches(c.present); got != c.want {
				t.Fatalf("BearerMatches(%q) = %v; want %v", c.present, got, c.want)
			}
		})
	}
}

func TestSetTokenHashRejectsRubbish(t *testing.T) {
	s, _ := newStore(t)
	for _, bad := range []string{
		"",
		"deadbeef",                         // too short
		hashOf("x") + "00",                 // too long
		strings.Repeat("z", sha256.Size*2), // right length, not hex
	} {
		if err := s.SetTokenHash(bad); err == nil {
			t.Fatalf("SetTokenHash(%q) was accepted; want an error", bad)
		}
	}
	if s.HasTokenHash() {
		t.Fatal("a rejected hash must not be stored")
	}
}

func TestSetTokenHashNormalises(t *testing.T) {
	s, _ := newStore(t)
	// Upper case and surrounding whitespace are the shapes a hand-written curl
	// or a stray newline produces; they name the same token.
	if err := s.SetTokenHash("  " + strings.ToUpper(hashOf("tok")) + "\n"); err != nil {
		t.Fatalf("SetTokenHash: %v", err)
	}
	if !s.BearerMatches("tok") {
		t.Fatal("an upper-case, padded hash should match the same token")
	}
}

// TestSetTriggersReplacesTheWholeSet is the one that matters for security: a
// trigger the user deleted must stop waking the workspace.
func TestSetTriggersReplacesTheWholeSet(t *testing.T) {
	s, dir := newStore(t)
	if err := s.SetTriggers([]Trigger{
		{Kind: "webhook", ID: "keep"},
		{Kind: "webhook", ID: "doomed"},
	}); err != nil {
		t.Fatalf("SetTriggers: %v", err)
	}
	// The workspace deleted "doomed" and re-pushes what it still has.
	if err := s.SetTriggers([]Trigger{{Kind: "webhook", ID: "keep"}}); err != nil {
		t.Fatalf("SetTriggers: %v", err)
	}
	if _, ok := s.Lookup("webhook", "doomed"); ok {
		t.Fatal("a deleted webhook is still known, so it could still wake the workspace")
	}
	if _, ok := s.Lookup("webhook", "keep"); !ok {
		t.Fatal("the surviving webhook was dropped too")
	}
	// And the removal is durable, not just in memory.
	reopened, err := NewStore(dir)
	if err != nil {
		t.Fatalf("reopen: %v", err)
	}
	if _, ok := reopened.Lookup("webhook", "doomed"); ok {
		t.Fatal("the deleted webhook came back after a restart")
	}
}

func TestSetTriggersRejectsRubbish(t *testing.T) {
	cases := []struct {
		name string
		trig Trigger
	}{
		{"unknown kind", Trigger{Kind: "pagewatch", ID: "a"}},
		{"empty kind", Trigger{Kind: "", ID: "a"}},
		{"empty id", Trigger{Kind: "webhook", ID: ""}},
		{"a slash", Trigger{Kind: "webhook", ID: "a/b"}},
		{"a dot-dot", Trigger{Kind: "webhook", ID: ".."}},
		{"a percent escape", Trigger{Kind: "webhook", ID: "a%2fb"}},
		{"a space", Trigger{Kind: "webhook", ID: "a b"}},
		{"too long", Trigger{Kind: "webhook", ID: strings.Repeat("a", 65)}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			s, _ := newStore(t)
			if err := s.SetTriggers([]Trigger{c.trig}); err == nil {
				t.Fatalf("SetTriggers(%+v) was accepted; want an error", c.trig)
			}
			if n := s.TriggerCount(); n != 0 {
				t.Fatalf("a rejected push stored %d triggers; want 0", n)
			}
		})
	}
}

// TestLookupDoesNotConfuseKinds: the three id spaces are separate, so a cron id
// presented at the webhook path must not resolve.
func TestLookupDoesNotConfuseKinds(t *testing.T) {
	s, _ := newStore(t)
	if err := s.SetTriggers([]Trigger{{Kind: "cron", ID: "nightly"}}); err != nil {
		t.Fatalf("SetTriggers: %v", err)
	}
	if _, ok := s.Lookup("webhook", "nightly"); ok {
		t.Fatal("a cron id resolved as a webhook")
	}
	if _, ok := s.Lookup("page-watch", "nightly"); ok {
		t.Fatal("a cron id resolved as a page-watch")
	}
	if _, ok := s.Lookup("cron", "nightly"); !ok {
		t.Fatal("the cron id did not resolve as a cron")
	}
}

func TestValidTriggerID(t *testing.T) {
	for _, ok := range []string{"a", "A", "0", "gh-pr", "a_b-C9", strings.Repeat("a", 64)} {
		if !validTriggerID(ok) {
			t.Fatalf("validTriggerID(%q) = false; want true", ok)
		}
	}
	for _, bad := range []string{"", ".", "..", "a/b", "a.b", "a%b", "a b", "a\nb", "a\x00b", strings.Repeat("a", 65)} {
		if validTriggerID(bad) {
			t.Fatalf("validTriggerID(%q) = true; want false", bad)
		}
	}
}

func TestWakesRoundTrip(t *testing.T) {
	s, dir := newStore(t)
	if got := s.Wakes(); len(got) != 0 {
		t.Fatalf("fresh wakes = %v; want empty", got)
	}
	if err := s.SetWakes([]int64{100, 200, 300}); err != nil {
		t.Fatalf("SetWakes: %v", err)
	}
	// The budget has to survive a restart, or a crashloop would be an
	// unlimited-wake primitive.
	reopened, err := NewStore(dir)
	if err != nil {
		t.Fatalf("reopen: %v", err)
	}
	got := reopened.Wakes()
	if len(got) != 3 || got[0] != 100 || got[2] != 300 {
		t.Fatalf("wakes after restart = %v; want [100 200 300]", got)
	}
	// The slice handed out must be a copy, or a caller could rewrite history.
	got[0] = 999
	if again := reopened.Wakes(); again[0] != 100 {
		t.Fatalf("Wakes() handed out its own slice; mutation leaked: %v", again)
	}
}

func TestNewStoreNeedsADirectory(t *testing.T) {
	if _, err := NewStore(""); err == nil {
		t.Fatal("NewStore(\"\") was accepted; a missing mount should be loud")
	}
}
