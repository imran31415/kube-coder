package main

import (
	"context"
	"encoding/json"
	"errors"
	"testing"
)

func mf(manager, fields string) ManagedField {
	return ManagedField{Manager: manager, FieldsV1: json.RawMessage(fields)}
}

const ownsReplicas = `{"f:spec":{"f:replicas":{}}}`

func TestClassify(t *testing.T) {
	i := func(n int32) *int32 { return &n }
	cases := []struct {
		name     string
		replicas *int32
		ready    int32
		fields   []ManagedField
		want     string
	}{
		{"replicas omitted, pod ready", nil, 1, nil, StateAwake},
		{"replicas omitted, no pod yet", nil, 0, nil, StateStarting},
		{"running", i(1), 1, nil, StateAwake},
		{"restarting", i(1), 0, nil, StateStarting},
		{"keeper put it to 0", i(0), 0, []ManagedField{mf(FieldManager, ownsReplicas)}, StateAsleep},
		{"admin put it to 0 (no owner left)", i(0), 0, nil, StateStopped},
		{"someone else owns replicas", i(0), 0, []ManagedField{mf("kubectl", ownsReplicas)}, StateStopped},
		{"shared with another owner", i(0), 0, []ManagedField{mf(FieldManager, ownsReplicas), mf("helm", ownsReplicas)}, StateStopped},
		{"keeper owns other fields only", i(0), 0, []ManagedField{mf(FieldManager, `{"f:spec":{"f:template":{}}}`)}, StateStopped},
		{"status replicas are not spec replicas", i(0), 0, []ManagedField{
			mf(FieldManager, ownsReplicas), mf("kube-controller-manager", `{"f:status":{"f:replicas":{}}}`)}, StateAsleep},
		{"unreadable fieldsV1 is skipped", i(0), 0, []ManagedField{mf(FieldManager, ownsReplicas), mf("x", `not json`)}, StateAsleep},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			var d Deployment
			d.Spec.Replicas, d.Status.ReadyReplicas, d.Metadata.ManagedFields = c.replicas, c.ready, c.fields
			if got := Classify(&d); got != c.want {
				t.Fatalf("Classify = %q, want %q", got, c.want)
			}
		})
	}
}

func TestWakeAsleep(t *testing.T) {
	f, k := newFake(t, 0, 0, FieldManager)
	state, woke, err := Wake(context.Background(), k)
	if err != nil || state != StateStarting || !woke {
		t.Fatalf("Wake = %q, %v, %v; want starting, true, nil", state, woke, err)
	}
	if len(f.scales) != 1 || f.scales[0].Replicas != 1 || f.scales[0].RV != "100" {
		t.Fatalf("scales = %+v; want one apply of 1 at rv 100", f.scales)
	}
	if got := f.scales[0]; got.ContentType != "application/apply-patch+yaml" || got.Query != "fieldManager=kube-coder-keeper&force=true" {
		t.Fatalf("scale request = %+v; want a forced apply under the keeper's field manager", got)
	}
}

func TestWakeLeavesOthersAlone(t *testing.T) {
	cases := []struct {
		name     string
		replicas int32
		ready    int32
		owners   []string
		want     string
	}{
		{"stopped by an admin", 0, 0, nil, StateStopped},
		{"awake", 1, 1, nil, StateAwake},
		{"starting", 1, 0, []string{FieldManager}, StateStarting},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			f, k := newFake(t, c.replicas, c.ready, c.owners...)
			state, woke, err := Wake(context.Background(), k)
			if err != nil || state != c.want || woke {
				t.Fatalf("Wake = %q, %v, %v; want %q, false, nil", state, woke, err, c.want)
			}
			if len(f.scales) != 0 {
				t.Fatalf("Wake wrote %+v; want no write", f.scales)
			}
		})
	}
}

// An admin Stop that lands between the keeper's read and its write must win:
// the write is refused, and the re-read sees a stopped workspace.
func TestWakeLosesToAConcurrentStop(t *testing.T) {
	f, k := newFake(t, 0, 0, FieldManager) // asleep
	// The race needs a write in between that leaves it at 0 but not ours:
	// Start then Stop by an admin.
	f.set(func(f *fakeAPI) {
		f.beforeScale = func(f *fakeAPI) { f.adminScale(1); f.adminScale(0) }
	})
	state, woke, err := Wake(context.Background(), k)
	if err != nil || state != StateStopped || woke {
		t.Fatalf("Wake = %q, %v, %v; want stopped, false, nil", state, woke, err)
	}
	if f.replicas == nil || *f.replicas != 0 {
		t.Fatalf("replicas = %v; the admin's 0 must stand", f.replicas)
	}
}

func TestWakeGivesUpAfterRepeatedConflicts(t *testing.T) {
	f, k := newFake(t, 0, 0, FieldManager)
	var bump func(f *fakeAPI)
	bump = func(f *fakeAPI) { f.rv++; f.beforeScale = bump } // someone writes before every attempt
	f.set(func(f *fakeAPI) { f.beforeScale = bump })
	if _, _, err := Wake(context.Background(), k); !errors.Is(err, ErrConflict) {
		t.Fatalf("Wake err = %v; want ErrConflict", err)
	}
	if len(f.scales) != 3 {
		t.Fatalf("attempts = %d; want 3", len(f.scales))
	}
}

func TestSleep(t *testing.T) {
	t.Run("awake", func(t *testing.T) {
		f, k := newFake(t, 1, 1, "helm")
		state, err := Sleep(context.Background(), k)
		if err != nil || state != StateAsleep {
			t.Fatalf("Sleep = %q, %v; want asleep, nil", state, err)
		}
		if len(f.scales) != 1 || f.scales[0].Replicas != 0 {
			t.Fatalf("scales = %+v; want one apply of 0", f.scales)
		}
		// …and what it wrote reads back as asleep, so a visit can wake it.
		d, _ := k.Get(context.Background())
		if got := Classify(d); got != StateAsleep {
			t.Fatalf("after Sleep, Classify = %q; want asleep", got)
		}
	})
	t.Run("already asleep", func(t *testing.T) {
		f, k := newFake(t, 0, 0, FieldManager)
		if state, err := Sleep(context.Background(), k); err != nil || state != StateAsleep {
			t.Fatalf("Sleep = %q, %v; want asleep, nil", state, err)
		}
		if len(f.scales) != 0 {
			t.Fatalf("Sleep wrote %+v; want no write", f.scales)
		}
	})
	t.Run("stopped by an admin", func(t *testing.T) {
		f, k := newFake(t, 0, 0)
		state, err := Sleep(context.Background(), k)
		if !errors.Is(err, errNotOurs) || state != StateStopped {
			t.Fatalf("Sleep = %q, %v; want stopped, errNotOurs", state, err)
		}
		if len(f.scales) != 0 {
			t.Fatalf("Sleep wrote %+v; turning a Stop into a sleep would let a visit undo it", f.scales)
		}
	})
}

// The full cycle the chart promises: sleep, visit wakes; admin stop, visit
// does not.
func TestSleepWakeStopCycle(t *testing.T) {
	f, k := newFake(t, 1, 1)
	ctx := context.Background()
	if _, err := Sleep(ctx, k); err != nil {
		t.Fatal(err)
	}
	if state, woke, _ := Wake(ctx, k); state != StateStarting || !woke {
		t.Fatalf("wake after keeper sleep = %q, %v; want starting, true", state, woke)
	}
	f.set(func(f *fakeAPI) { f.ready = 1; f.adminScale(0); f.ready = 0 })
	if state, woke, _ := Wake(ctx, k); state != StateStopped || woke {
		t.Fatalf("wake after admin stop = %q, %v; want stopped, false", state, woke)
	}
}
