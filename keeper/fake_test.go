package main

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"sync"
	"testing"
)

// fakeAPI is a one-Deployment API server that keeps managedFields the way the
// real one was measured to (docs/always-on-keeper.md, step 3 spike): a keeper
// apply on /scale makes the keeper the sole owner of spec.replicas, and an
// admin `kubectl scale` that changes the value takes it away.
type fakeAPI struct {
	t *testing.T

	mu        sync.Mutex
	replicas  *int32
	ready     int32
	rv        int
	owners    []string // managers owning f:spec.f:replicas
	gets      int
	scales    []scaleCall
	conflicts int // scale writes refused with 409
	// beforeScale, if set, runs (locked) before a scale is applied — a
	// competing writer landing between the keeper's read and its write.
	beforeScale func(f *fakeAPI)
	failGet     int // HTTP status to fail GETs with, 0 = none
}

type scaleCall struct {
	Replicas    int32
	RV          string
	ContentType string
	Query       string
	Auth        string
}

func newFake(t *testing.T, replicas int32, ready int32, owners ...string) (*fakeAPI, *Kube) {
	t.Helper()
	f := &fakeAPI{t: t, replicas: &replicas, ready: ready, rv: 100, owners: owners}
	srv := httptest.NewTLSServer(f)
	t.Cleanup(srv.Close)
	token := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(token, []byte("tok-1\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	return f, &Kube{BaseURL: srv.URL, Client: srv.Client(), TokenFile: token, Namespace: "ws-ns", Name: "ws-alice"}
}

const fakePath = "/apis/apps/v1/namespaces/ws-ns/deployments/ws-alice"

// adminScale is `kubectl scale` from the test: an update that changes the
// value strips every owner (and a 0 -> 0 one changes nothing at all).
func (f *fakeAPI) adminScale(n int32) {
	if f.replicas != nil && *f.replicas == n {
		return
	}
	f.replicas, f.owners = &n, nil
	f.rv++
}

// controllerApply is the controller's Stop on a sleeping workspace (#728 step
// 5): a forced server-side apply of replicas under its own field manager.
// Unlike adminScale it records ownership even when the value does not change,
// which is the whole reason the controller uses it for a 0 -> 0 Stop. And, as
// measured on a real cluster, a same-value apply SHARES the field rather than
// taking it — there is no conflict to force — so the keeper stays an owner
// next to the controller. A different value is a conflict, and the force takes
// the field outright.
func (f *fakeAPI) controllerApply(n int32) {
	const manager = "kube-coder-controller"
	switch {
	case f.replicas == nil || *f.replicas != n:
		f.replicas, f.owners = &n, []string{manager}
	case !slices.Contains(f.owners, manager):
		f.owners = append(f.owners, manager)
	}
	f.rv++
}

func (f *fakeAPI) set(fn func(f *fakeAPI)) {
	f.mu.Lock()
	defer f.mu.Unlock()
	fn(f)
}

func (f *fakeAPI) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	switch {
	case r.Method == http.MethodGet && r.URL.Path == fakePath:
		f.gets++
		if f.failGet != 0 {
			http.Error(w, `{"kind":"Status","message":"boom"}`, f.failGet)
			return
		}
		_ = json.NewEncoder(w).Encode(f.deployment())
	case r.Method == http.MethodPatch && r.URL.Path == fakePath+"/scale":
		body, _ := io.ReadAll(r.Body)
		var s struct {
			APIVersion string `json:"apiVersion"`
			Kind       string `json:"kind"`
			Metadata   struct {
				Name, Namespace, ResourceVersion string
			} `json:"metadata"`
			Spec struct {
				Replicas *int32 `json:"replicas"`
			} `json:"spec"`
		}
		if err := json.Unmarshal(body, &s); err != nil || s.Spec.Replicas == nil ||
			s.APIVersion != "autoscaling/v1" || s.Kind != "Scale" ||
			s.Metadata.Name != "ws-alice" || s.Metadata.Namespace != "ws-ns" {
			f.t.Errorf("bad apply body: %s", body)
			http.Error(w, "bad body", http.StatusBadRequest)
			return
		}
		f.scales = append(f.scales, scaleCall{
			Replicas: *s.Spec.Replicas, RV: s.Metadata.ResourceVersion,
			ContentType: r.Header.Get("Content-Type"), Query: r.URL.RawQuery,
			Auth: r.Header.Get("Authorization"),
		})
		if f.beforeScale != nil {
			hook := f.beforeScale
			f.beforeScale = nil
			hook(f)
		}
		if s.Metadata.ResourceVersion != "" && s.Metadata.ResourceVersion != strconv.Itoa(f.rv) {
			f.conflicts++
			w.WriteHeader(http.StatusConflict)
			_, _ = w.Write([]byte(`{"kind":"Status","reason":"Conflict","code":409}`))
			return
		}
		f.replicas, f.owners = s.Spec.Replicas, []string{FieldManager}
		f.rv++
		_, _ = w.Write([]byte(`{}`))
	default:
		f.t.Errorf("unexpected %s %s", r.Method, r.URL.Path)
		http.NotFound(w, r)
	}
}

func (f *fakeAPI) deployment() map[string]any {
	var mf []map[string]any
	for _, o := range f.owners {
		mf = append(mf, map[string]any{
			"manager": o, "operation": "Apply", "subresource": "scale", "apiVersion": "apps/v1",
			"fieldsV1": map[string]any{"f:spec": map[string]any{"f:replicas": map[string]any{}}},
		})
	}
	// Owners of other fields only, as helm and the deployment controller are.
	mf = append(mf,
		map[string]any{"manager": "helm", "operation": "Update", "fieldsV1": map[string]any{
			"f:spec": map[string]any{"f:template": map[string]any{}}}},
		map[string]any{"manager": "kube-controller-manager", "operation": "Update", "subresource": "status",
			"fieldsV1": map[string]any{"f:status": map[string]any{"f:replicas": map[string]any{}}}},
	)
	spec := map[string]any{}
	if f.replicas != nil {
		spec["replicas"] = *f.replicas
	}
	return map[string]any{
		"metadata": map[string]any{"name": "ws-alice", "resourceVersion": strconv.Itoa(f.rv), "managedFields": mf},
		"spec":     spec,
		"status":   map[string]any{"readyReplicas": f.ready},
	}
}
