package main

// What state the workspace is in, and the two actions on it.
//
// The one rule that matters: the keeper only wakes a workspace it put to sleep
// itself. A workspace an admin stopped from the controller stays stopped
// (#728 §3). The proof of "the keeper put it to sleep" is the API server's own
// record of who owns spec.replicas — the Deployment's managedFields:
//
//   - the keeper scales by server-side apply under FieldManager
//     ("kube-coder-keeper"), which records it as the field's owner;
//   - the controller's Stop is `kubectl scale --replicas=0`, an update that
//     changes the value, so the keeper stops being the owner;
//   - with keeper.sleep.enabled the chart omits `replicas` (step 2), so a
//     `helm upgrade` — Helm 3's patch or Helm 4's apply — leaves the field
//     and its owner alone.
//
// So "replicas is 0 and the keeper is its only owner" means asleep, and
// anything else at 0 means stopped. Neither a keeper-side record of the
// resourceVersion nor of the generation can carry this: the deployment
// controller's status writes move the resourceVersion seconds after every
// scale, and a release bump moves the generation of a sleeping workspace, so
// both would turn every sleeping workspace into "stopped" — silently, and in
// the direction that never wakes it.
//
// The one gap: a Stop on a workspace that is already asleep is 0 -> 0, which
// changes nothing, so the keeper still owns the field and the next visit wakes
// it. The controller only offers Stop on a running workspace, so only a hand
// typed `kubectl scale --replicas=0` can hit this (docs/always-on-keeper.md).

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
)

// States reported by GET /api/keeper/status and `keeper status`.
const (
	StateAwake    = "awake"    // replicas >= 1 and a pod is Ready
	StateStarting = "starting" // replicas >= 1, no Ready pod yet (restart, or a wake under way)
	StateAsleep   = "asleep"   // replicas == 0, written by the keeper — a visit wakes it
	StateStopped  = "stopped"  // replicas == 0, written by anyone else — left alone
)

// Classify reports the workspace's state from its Deployment.
func Classify(d *Deployment) string {
	replicas := int32(1) // the API server defaults an absent field to 1
	if d.Spec.Replicas != nil {
		replicas = *d.Spec.Replicas
	}
	switch {
	case replicas > 0 && d.Status.ReadyReplicas > 0:
		return StateAwake
	case replicas > 0:
		return StateStarting
	case keeperOwnsReplicas(d.Metadata.ManagedFields):
		return StateAsleep
	default:
		return StateStopped
	}
}

// keeperOwnsReplicas is true when the keeper is the only field manager that
// owns spec.replicas. Shared or unknown ownership counts as not ours.
func keeperOwnsReplicas(fields []ManagedField) bool {
	owners := replicasOwners(fields)
	return len(owners) == 1 && owners[0] == FieldManager
}

// replicasOwners lists every field manager whose fieldsV1 includes spec.replicas.
func replicasOwners(fields []ManagedField) []string {
	var owners []string
	for _, f := range fields {
		var set struct {
			Spec map[string]json.RawMessage `json:"f:spec"`
		}
		if json.Unmarshal(f.FieldsV1, &set) != nil {
			continue
		}
		if _, ok := set.Spec["f:replicas"]; ok {
			owners = append(owners, f.Manager)
		}
	}
	return owners
}

// errNotOurs is returned by Sleep for a workspace that is already at 0 because
// someone else put it there: turning that into "asleep" would let the next
// visit undo an admin's Stop.
var errNotOurs = errors.New("the workspace is stopped by someone else; leaving it stopped")

// Wake scales an asleep workspace back to 1 and reports the state it leaves
// the workspace in, and whether it wrote. Any other state is returned
// unchanged with no write: an awake or starting workspace needs nothing, and a
// stopped one is not ours. The write carries the resourceVersion it decided
// on, so an admin Stop that lands in between wins (409), and the decision is
// re-made from fresh state.
func Wake(ctx context.Context, k *Kube) (state string, woke bool, err error) {
	for attempt := 0; attempt < 3; attempt++ {
		d, err := k.Get(ctx)
		if err != nil {
			return "", false, err
		}
		state := Classify(d)
		if state != StateAsleep {
			return state, false, nil
		}
		err = k.Scale(ctx, 1, d.Metadata.ResourceVersion)
		if errors.Is(err, ErrConflict) {
			continue
		}
		if err != nil {
			return "", false, err
		}
		return StateStarting, true, nil
	}
	return "", false, ErrConflict
}

// Sleep scales the workspace to 0 under the keeper's field manager. Step 5's
// idle loop will call this once GET /api/keeper/idle says the workspace is
// idle; until then it is the operator's `kubectl exec … /keeper sleep`, which
// is exactly as forceful as the controller's Stop: running agents are killed.
func Sleep(ctx context.Context, k *Kube) (string, error) {
	for attempt := 0; attempt < 3; attempt++ {
		d, err := k.Get(ctx)
		if err != nil {
			return "", err
		}
		switch Classify(d) {
		case StateAsleep:
			return StateAsleep, nil
		case StateStopped:
			return StateStopped, fmt.Errorf("%w (spec.replicas owned by %v)", errNotOurs, replicasOwners(d.Metadata.ManagedFields))
		}
		err = k.Scale(ctx, 0, d.Metadata.ResourceVersion)
		if errors.Is(err, ErrConflict) {
			continue
		}
		if err != nil {
			return "", err
		}
		return StateAsleep, nil
	}
	return "", ErrConflict
}
