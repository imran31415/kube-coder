package main

import (
	"context"
	"errors"
	"net/http"
	"os"
	"strings"
	"testing"
)

func TestGetDecodesManagedFields(t *testing.T) {
	_, k := newFake(t, 0, 0, FieldManager)
	d, err := k.Get(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if d.Metadata.ResourceVersion != "100" || d.Spec.Replicas == nil || *d.Spec.Replicas != 0 {
		t.Fatalf("decoded %+v", d)
	}
	if got := replicasOwners(d.Metadata.ManagedFields); len(got) != 1 || got[0] != FieldManager {
		t.Fatalf("replicas owners = %v; want [%s]", got, FieldManager)
	}
}

// Bound ServiceAccount tokens rotate under the pod: every call reads the file.
func TestTokenIsReReadEveryCall(t *testing.T) {
	f, k := newFake(t, 1, 1)
	ctx := context.Background()
	if err := k.Scale(ctx, 1, ""); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(k.TokenFile, []byte("tok-2"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := k.Scale(ctx, 1, ""); err != nil {
		t.Fatal(err)
	}
	if f.scales[0].Auth != "Bearer tok-1" || f.scales[1].Auth != "Bearer tok-2" {
		t.Fatalf("auth headers = %q, %q", f.scales[0].Auth, f.scales[1].Auth)
	}
}

func TestScaleMapsConflict(t *testing.T) {
	_, k := newFake(t, 0, 0, FieldManager)
	if err := k.Scale(context.Background(), 1, "1"); !errors.Is(err, ErrConflict) {
		t.Fatalf("stale resourceVersion: err = %v; want ErrConflict", err)
	}
}

func TestAPIErrorCarriesStatusMessage(t *testing.T) {
	f, k := newFake(t, 0, 0)
	f.set(func(f *fakeAPI) { f.failGet = http.StatusForbidden })
	_, err := k.Get(context.Background())
	if err == nil || !strings.Contains(err.Error(), "403") || !strings.Contains(err.Error(), "boom") {
		t.Fatalf("err = %v; want the status and the API's message", err)
	}
}

func TestInClusterNeedsTheServiceEnv(t *testing.T) {
	t.Setenv("KUBERNETES_SERVICE_HOST", "")
	if _, err := InCluster("ns", "ws-x"); err == nil {
		t.Fatal("InCluster outside a cluster: want an error")
	}
}
