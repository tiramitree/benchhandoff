package main

import (
	"testing"
	"time"

	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/tools/leaderelection/resourcelock"
)

func TestManagerOptionsFixNamespacedLeaseElection(t *testing.T) {
	scheme := runtime.NewScheme()
	options := managerOptions(scheme)

	if options.Scheme != scheme {
		t.Fatal("manager options did not retain the supplied scheme")
	}
	if !options.LeaderElection {
		t.Fatal("leader election is disabled")
	}
	if options.LeaderElectionResourceLock != resourcelock.LeasesResourceLock {
		t.Fatalf(
			"leader-election lock = %q, want %q",
			options.LeaderElectionResourceLock,
			resourcelock.LeasesResourceLock,
		)
	}
	if options.LeaderElectionID != leaderElectionID ||
		options.LeaderElectionNamespace != leaderElectionNamespace {
		t.Fatalf(
			"leader-election identity = %q/%q, want %q/%q",
			options.LeaderElectionNamespace,
			options.LeaderElectionID,
			leaderElectionNamespace,
			leaderElectionID,
		)
	}
	if options.LeaderElectionReleaseOnCancel {
		t.Fatal("leader election releases its Lease before manager shutdown")
	}
	if options.LeaseDuration == nil ||
		*options.LeaseDuration != 15*time.Second {
		t.Fatalf("Lease duration = %v, want 15s", options.LeaseDuration)
	}
	if options.RenewDeadline == nil ||
		*options.RenewDeadline != 10*time.Second {
		t.Fatalf("renew deadline = %v, want 10s", options.RenewDeadline)
	}
	if options.RetryPeriod == nil ||
		*options.RetryPeriod != 2*time.Second {
		t.Fatalf("retry period = %v, want 2s", options.RetryPeriod)
	}
}
