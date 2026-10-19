package main

// A minimal in-cluster Kubernetes client: the keeper needs exactly two calls
// (read its workspace Deployment, write that Deployment's scale), so it speaks
// to the API server directly instead of pulling in client-go. That keeps the
// binary small, the idle RSS well under the 20 MiB budget in #728, and the
// dependency list empty.

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"
)

// FieldManager is the name the keeper writes `spec.replicas` under. Kubernetes
// records it in the Deployment's managedFields, which is how the keeper later
// tells a workspace it put to sleep from one an admin stopped (see state.go).
const FieldManager = "kube-coder-keeper"

const saDir = "/var/run/secrets/kubernetes.io/serviceaccount"

// ErrConflict is a 409 from the API server: the resourceVersion precondition
// on a scale write no longer matches, i.e. someone else wrote in between.
var ErrConflict = errors.New("conflict: the deployment changed since it was read")

// Deployment is the slice of apps/v1 Deployment the keeper reads.
type Deployment struct {
	Metadata struct {
		Name            string         `json:"name"`
		ResourceVersion string         `json:"resourceVersion"`
		ManagedFields   []ManagedField `json:"managedFields"`
	} `json:"metadata"`
	Spec struct {
		Replicas *int32 `json:"replicas"`
	} `json:"spec"`
	Status struct {
		ReadyReplicas int32 `json:"readyReplicas"`
	} `json:"status"`
}

// ManagedField is one managedFields entry: who last wrote which fields.
type ManagedField struct {
	Manager     string          `json:"manager"`
	Operation   string          `json:"operation"`
	Subresource string          `json:"subresource"`
	FieldsV1    json.RawMessage `json:"fieldsV1"`
}

// Kube talks to the API server about one Deployment.
type Kube struct {
	BaseURL   string // https://host:port, no trailing slash
	Client    *http.Client
	TokenFile string // re-read on every call: bound tokens rotate
	Namespace string
	Name      string
}

// InCluster builds a Kube from the pod's ServiceAccount mount and the
// KUBERNETES_SERVICE_* env the kubelet injects.
func InCluster(namespace, name string) (*Kube, error) {
	host, port := os.Getenv("KUBERNETES_SERVICE_HOST"), os.Getenv("KUBERNETES_SERVICE_PORT")
	if host == "" || port == "" {
		return nil, errors.New("not running in a cluster: KUBERNETES_SERVICE_HOST/PORT unset")
	}
	ca, err := os.ReadFile(saDir + "/ca.crt")
	if err != nil {
		return nil, fmt.Errorf("read service account CA: %w", err)
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(ca) {
		return nil, errors.New("service account CA has no certificates")
	}
	return &Kube{
		BaseURL: "https://" + net.JoinHostPort(host, port),
		Client: &http.Client{
			Timeout:   10 * time.Second,
			Transport: &http.Transport{TLSClientConfig: &tls.Config{RootCAs: pool, MinVersion: tls.VersionTLS12}},
		},
		TokenFile: saDir + "/token",
		Namespace: namespace,
		Name:      name,
	}, nil
}

func (k *Kube) deploymentPath() string {
	return "/apis/apps/v1/namespaces/" + url.PathEscape(k.Namespace) + "/deployments/" + url.PathEscape(k.Name)
}

// Get reads the Deployment, managedFields included.
func (k *Kube) Get(ctx context.Context) (*Deployment, error) {
	body, err := k.do(ctx, http.MethodGet, k.deploymentPath(), "", nil)
	if err != nil {
		return nil, err
	}
	var d Deployment
	if err := json.Unmarshal(body, &d); err != nil {
		return nil, fmt.Errorf("decode deployment: %w", err)
	}
	return &d, nil
}

// Scale sets spec.replicas through the scale subresource — the only write the
// keeper's Role allows.
//
// It is a server-side apply, not a merge patch, because only an apply leaves a
// record: on the scale subresource a plain update (merge patch, `kubectl
// scale`) that sets replicas to 0 drops every owner of the field, so a merge
// patch could never tell a keeper sleep from an admin Stop. An apply under
// FieldManager stays recorded as the owner until someone else changes the
// value. force takes the field over from whoever held it (helm, an admin
// Start); state.go only ever does that after deciding from fresh state.
//
// resourceVersion is a precondition: if the Deployment changed since it was
// read (an admin Stop, a helm upgrade), the API server refuses with 409 and
// Scale returns ErrConflict instead of overwriting it.
func (k *Kube) Scale(ctx context.Context, replicas int32, resourceVersion string) error {
	meta := map[string]any{"name": k.Name, "namespace": k.Namespace}
	if resourceVersion != "" {
		meta["resourceVersion"] = resourceVersion
	}
	b, err := json.Marshal(map[string]any{
		"apiVersion": "autoscaling/v1",
		"kind":       "Scale",
		"metadata":   meta,
		"spec":       map[string]any{"replicas": replicas},
	})
	if err != nil {
		return err
	}
	path := k.deploymentPath() + "/scale?fieldManager=" + url.QueryEscape(FieldManager) + "&force=true"
	// JSON is YAML, so the apply body needs no YAML encoder.
	_, err = k.do(ctx, http.MethodPatch, path, "application/apply-patch+yaml", b)
	return err
}

func (k *Kube) do(ctx context.Context, method, path, contentType string, body []byte) ([]byte, error) {
	token, err := os.ReadFile(k.TokenFile)
	if err != nil {
		return nil, fmt.Errorf("read service account token: %w", err)
	}
	req, err := http.NewRequestWithContext(ctx, method, k.BaseURL+path, bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+strings.TrimSpace(string(token)))
	req.Header.Set("Accept", "application/json")
	if contentType != "" {
		req.Header.Set("Content-Type", contentType)
	}
	resp, err := k.Client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	b, err := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	if err != nil {
		return nil, err
	}
	switch {
	case resp.StatusCode == http.StatusConflict:
		return nil, ErrConflict
	case resp.StatusCode >= 300:
		return nil, fmt.Errorf("%s %s: %s: %s", method, path, resp.Status, apiMessage(b))
	}
	return b, nil
}

// apiMessage pulls the message out of a metav1.Status body for error text.
func apiMessage(b []byte) string {
	var s struct {
		Message string `json:"message"`
	}
	if json.Unmarshal(b, &s) == nil && s.Message != "" {
		return s.Message
	}
	if len(b) > 200 {
		b = b[:200]
	}
	return strings.TrimSpace(string(b))
}
