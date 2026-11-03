// Command keeper is the always-on half of a kube-coder workspace (#728).
//
// Steps 3 to 5 of docs/always-on-keeper.md §5: it stands in for the holding
// page while the workspace is away, wakes an asleep workspace (0 -> 1) for a
// logged-in visit (step 3), the phone app (step 4a) or a webhook or schedule
// (step 4b), and with keeper.sleep.enabled puts it to sleep (1 -> 0) once it
// has been idle for keeper.sleep.idleMinutes (step 5). It never wakes a
// workspace an admin stopped. An operator can also sleep it by hand:
//
//	kubectl -n <ns> exec deploy/keeper-<user> -- /keeper sleep
//
// One listener per class of caller, so the chart's NetworkPolicy can admit each
// from exactly where it should come from:
//
//	:8080  the page         anyone through the ingress; never touches the API
//	:8081  browser wake     the ingress, behind basic auth or oauth2-proxy
//	:8082  the app          the ingress, checked against the token hash
//	:8083  the workspace     trigger ids and the token hash, shared secret
//
// Usage:
//
//	keeper [serve]   run the listeners
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
	"path/filepath"
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
	s := &Server{
		Kube:              k,
		PageFile:          page,
		UserHeader:        os.Getenv("KEEPER_USER_HEADER"),
		ControlSecretFile: os.Getenv("KEEPER_CONTROL_SECRET_FILE"),
		WorkspaceURL:      os.Getenv("KEEPER_WORKSPACE_URL"),
		SleepEnabled:      os.Getenv("KEEPER_SLEEP_ENABLED") == "true",
		IdleMinutes:       idleMinutesFromEnv(),
		cacheFor:          time.Second,
	}
	servers := []*http.Server{
		newHTTPServer(":8080", s.PageHandler()),
		newHTTPServer(":8081", s.WakeHandler()),
	}
	listening := "page :8080, wake :8081"
	// One context for the background loops, cancelled on SIGTERM before the
	// listeners shut down, so neither a replay nor a sleep starts mid-exit.
	ctx, stopLoops := context.WithCancel(context.Background())
	defer stopLoops()

	// Step 4's listeners run only once the chart has given the keeper somewhere
	// to keep what the workspace pushes. Without that the keeper is exactly
	// step 3, so this image on an older chart keeps working rather than
	// crashlooping on a volume that isn't mounted yet.
	if dir := os.Getenv("KEEPER_STATE_DIR"); dir != "" {
		store, serr := NewStore(dir)
		if serr != nil {
			return serr
		}
		s.Store = store
		// The queue and the wake budget, step 4b. Both live on the same volume
		// as the store, and both are read back here: a keeper that restarted
		// while the workspace slept must come back holding the requests it
		// accepted and the budget it had already spent.
		queue, qerr := NewQueue(filepath.Join(dir, "queue"), queueLimitsFromEnv())
		if qerr != nil {
			return qerr
		}
		s.Queue = queue
		s.Limiter = NewWakeLimiter(envInt("KEEPER_MAX_WAKES_PER_HOUR", 0), store)
		if n := envInt("KEEPER_MAX_INFLIGHT", 4); n > 0 {
			s.forwards = make(chan struct{}, n)
		}
		servers = append(servers,
			newHTTPServer(":8082", s.ProxyHandler()),
			newHTTPServer(":8083", s.ControlHandler()),
		)
		listening += ", app and webhooks :8082, control :8083"
		held := "not pushed yet, so the app cannot wake this workspace"
		if store.HasTokenHash() {
			held = "present"
		}
		log.Printf("store: %d trigger ids; token hash %s", store.TriggerCount(), held)
		go s.DrainLoop(ctx)
	} else {
		log.Printf("step 4 is off (KEEPER_STATE_DIR unset): webhooks, crons and the app cannot wake this workspace; a logged-in visit still can")
	}
	// The idle loop (step 5). Read once, here: turning sleep off changes the
	// Deployment's env, which restarts this pod anyway.
	if s.SleepEnabled {
		go s.IdleLoop(ctx)
		listening += fmt.Sprintf("; sleeps after %d idle minutes", s.IdleMinutes)
	}
	log.Printf("keeper %s: looking after %s/%s; %s", version, k.Namespace, k.Name, listening)
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
	stopLoops()
	shutdown, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	for _, srv := range servers {
		_ = srv.Shutdown(shutdown)
	}
	if errors.Is(err, http.ErrServerClosed) {
		err = nil
	}
	return err
}

// idleMinutesFromEnv is keeper.sleep.idleMinutes. The chart refuses anything
// below 1 at render time; a keeper run some other way that is given 0 must not
// read it as "sleep the moment it is quiet", so that falls back to the default.
func idleMinutesFromEnv() int {
	n := envInt("KEEPER_IDLE_MINUTES", 30)
	if n < 1 {
		log.Printf("KEEPER_IDLE_MINUTES=%d would sleep a workspace the moment it went quiet; using 30", n)
		return 30
	}
	return n
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
