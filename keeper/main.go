// Command keeper is the always-on half of a kube-coder workspace (#728).
//
// Step 3 of docs/always-on-keeper.md §5: it stands in for the holding page
// while the workspace is away, and a logged-in visit to an asleep workspace
// wakes it (0 -> 1). It never wakes a workspace an admin stopped. Webhooks,
// crons and the idle loop that puts the workspace to sleep are later steps;
// for now an operator puts it to sleep by hand:
//
//	kubectl -n <ns> exec deploy/keeper-<user> -- /keeper sleep
//
// Usage:
//
//	keeper [serve]   run the page (:8080) and wake (:8081) listeners
//	keeper sleep     scale the workspace to 0 as the keeper
//	keeper status    print awake | starting | asleep | stopped
//	keeper version
package main

import (
	"context"
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"
)

var version = "dev" // -ldflags "-X main.version=…" in keeper/Dockerfile

func main() {
	cmd := "serve"
	if len(os.Args) > 1 {
		cmd = os.Args[1]
	}
	var err error
	switch cmd {
	case "serve":
		err = serve()
	case "sleep":
		if os.Getenv("KEEPER_SLEEP_ENABLED") != "true" {
			err = errors.New("keeper.sleep.enabled is off: the chart still sets the workspace's replicas, so the next helm upgrade would wake it again")
			break
		}
		err = oneShot(Sleep)
	case "status":
		err = oneShot(func(ctx context.Context, k *Kube) (string, error) {
			d, err := k.Get(ctx)
			if err != nil {
				return "", err
			}
			return Classify(d), nil
		})
	case "version", "-version", "--version":
		fmt.Println(version)
	default:
		fmt.Fprintf(os.Stderr, "usage: keeper [serve|sleep|status|version]\n")
		os.Exit(2)
	}
	if err != nil {
		fmt.Fprintf(os.Stderr, "keeper %s: %v\n", cmd, err)
		os.Exit(1)
	}
}

// workspace reads which Deployment this keeper looks after. The chart sets
// both: the namespace from the downward API, the name as ws-<user>.
func workspace() (*Kube, error) {
	ns, name := os.Getenv("KEEPER_NAMESPACE"), os.Getenv("KEEPER_DEPLOYMENT")
	if ns == "" || name == "" {
		return nil, errors.New("KEEPER_NAMESPACE and KEEPER_DEPLOYMENT must be set")
	}
	return InCluster(ns, name)
}

func oneShot(f func(context.Context, *Kube) (string, error)) error {
	k, err := workspace()
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	state, err := f(ctx, k)
	if state != "" {
		fmt.Println(state)
	}
	return err
}

func serve() error {
	k, err := workspace()
	if err != nil {
		return err
	}
	page := os.Getenv("KEEPER_PAGE")
	if page == "" {
		page = "/etc/keeper/page/holding.html"
	}
	s := &Server{Kube: k, PageFile: page, UserHeader: os.Getenv("KEEPER_USER_HEADER"), cacheFor: time.Second}
	servers := []*http.Server{
		newHTTPServer(":8080", s.PageHandler()),
		newHTTPServer(":8081", s.WakeHandler()),
	}
	log.Printf("keeper %s: looking after %s/%s; page :8080, wake :8081", version, k.Namespace, k.Name)
	go startupCheck(k)

	errc := make(chan error, len(servers))
	for _, srv := range servers {
		go func() { errc <- srv.ListenAndServe() }()
	}
	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGTERM, os.Interrupt)
	select {
	case err = <-errc: // a listener died: exit so the kubelet restarts the pod
	case <-stop:
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	for _, srv := range servers {
		_ = srv.Shutdown(ctx)
	}
	if errors.Is(err, http.ErrServerClosed) {
		err = nil
	}
	return err
}

// startupCheck reads the workspace once and says what it found, so a broken
// Role or an unreachable API server shows in the first lines of the log rather
// than as the first visitor's failed wake. It never stops the keeper: the page
// still works without the API, and is what the ingress needs most.
func startupCheck(k *Kube) {
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	d, err := k.Get(ctx)
	if err != nil {
		log.Printf("startup check: cannot read %s/%s, so status and wake will fail until this is fixed (Role keeper-<user>, API server reachable?): %v", k.Namespace, k.Name, err)
		return
	}
	log.Printf("startup check: %s/%s is %s", k.Namespace, k.Name, Classify(d))
}

func newHTTPServer(addr string, h http.Handler) *http.Server {
	return &http.Server{
		Addr:              addr,
		Handler:           h,
		ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout:       30 * time.Second,
		WriteTimeout:      30 * time.Second,
		IdleTimeout:       90 * time.Second,
		MaxHeaderBytes:    64 << 10,
	}
}
