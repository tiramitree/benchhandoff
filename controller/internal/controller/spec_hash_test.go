package controller

import (
	"strings"
	"testing"

	agentrunv1alpha1 "github.com/tiramitree/benchhandoff/controller/api/v1alpha1"
)

func validExecutionSpec() agentrunv1alpha1.ExecutionSpec {
	return agentrunv1alpha1.ExecutionSpec{
		PVCName:               "bench-data",
		SuitePath:             "demo/suite.toml",
		SuiteSHA256:           strings.Repeat("b", 64),
		RunnerImage:           "ghcr.io/tiramitree/benchhandoff-runner@sha256:" + strings.Repeat("a", 64),
		ActiveDeadlineSeconds: 300,
	}
}

func TestCanonicalExecutionSpecSHA(t *testing.T) {
	spec := validExecutionSpec()
	got, err := CanonicalExecutionSpecSHA(spec)
	if err != nil {
		t.Fatalf("CanonicalExecutionSpecSHA: %v", err)
	}
	const want = "80efaa89b6dff938ace8f7597fdef47e241738b3e8cf2c61b2d571c83cc558ad"
	if got != want {
		t.Fatalf("canonical hash = %q, want %q", got, want)
	}

	changed := spec
	changed.ActiveDeadlineSeconds++
	other, err := CanonicalExecutionSpecSHA(changed)
	if err != nil {
		t.Fatalf("changed CanonicalExecutionSpecSHA: %v", err)
	}
	if other == got {
		t.Fatal("different execution specs produced the same canonical hash")
	}

	changedSuite := spec
	changedSuite.SuiteSHA256 = strings.Repeat("c", 64)
	otherSuite, err := CanonicalExecutionSpecSHA(changedSuite)
	if err != nil {
		t.Fatalf("suite-changed CanonicalExecutionSpecSHA: %v", err)
	}
	if otherSuite == got {
		t.Fatal("different suite bytes produced the same canonical execution hash")
	}
}

func TestResolveSuitePath(t *testing.T) {
	got, err := ResolveSuitePath("nested/suite.toml")
	if err != nil {
		t.Fatalf("ResolveSuitePath: %v", err)
	}
	if got != "/benchhandoff-data/suites/nested/suite.toml" {
		t.Fatalf("resolved path = %q", got)
	}

	invalid := []string{
		"",
		".",
		"suite",
		"suite.json",
		"/suite.toml",
		"../suite.toml",
		"a/../suite.toml",
		"a//suite.toml",
		"a\\suite.toml",
		"a/suite.toml/",
		" a/suite.toml",
		"a/suite.toml ",
		"C:/suite.toml",
		".hidden/suite.toml",
		strings.Repeat("a", 508) + ".toml",
	}
	for _, value := range invalid {
		t.Run(value, func(t *testing.T) {
			if _, err := ResolveSuitePath(value); err == nil {
				t.Fatalf("ResolveSuitePath(%q) unexpectedly succeeded", value)
			}
		})
	}
}

func TestValidateExecutionSpecBoundaries(t *testing.T) {
	for _, deadline := range []int64{30, 1800} {
		spec := validExecutionSpec()
		spec.ActiveDeadlineSeconds = deadline
		if err := ValidateExecutionSpec(spec); err != nil {
			t.Fatalf("deadline %d rejected: %v", deadline, err)
		}
	}
	for _, deadline := range []int64{-1, 0, 29, 1801} {
		spec := validExecutionSpec()
		spec.ActiveDeadlineSeconds = deadline
		if err := ValidateExecutionSpec(spec); err == nil {
			t.Fatalf("deadline %d unexpectedly accepted", deadline)
		}
	}
}

func TestValidateExecutionSpecRejectsInvalidSuiteDigest(t *testing.T) {
	for _, digest := range []string{
		"",
		strings.Repeat("b", 63),
		strings.Repeat("b", 65),
		strings.Repeat("B", 64),
		strings.Repeat("g", 64),
	} {
		t.Run(digest, func(t *testing.T) {
			spec := validExecutionSpec()
			spec.SuiteSHA256 = digest
			if err := ValidateExecutionSpec(spec); err == nil {
				t.Fatalf("suite digest %q unexpectedly accepted", digest)
			}
		})
	}
}

func TestValidateExecutionSpecRejectsUnpinnedOrAmbiguousImages(t *testing.T) {
	invalid := []string{
		"",
		"ghcr.io/tiramitree/runner:latest",
		"https://ghcr.io/tiramitree/runner@sha256:" + strings.Repeat("a", 64),
		"GHCR.io/tiramitree/runner@sha256:" + strings.Repeat("a", 64),
		"ghcr.io/tiramitree/runner@sha256:" + strings.Repeat("A", 64),
		"ghcr.io/tiramitree/runner@@sha256:" + strings.Repeat("a", 64),
		"ghcr.io//runner@sha256:" + strings.Repeat("a", 64),
		"ghcr.io/../runner@sha256:" + strings.Repeat("a", 64),
		"ghcr.io/tiramitree/runner@sha256:" + strings.Repeat("a", 63),
		strings.Repeat("a", 513),
	}
	for _, image := range invalid {
		t.Run(image, func(t *testing.T) {
			spec := validExecutionSpec()
			spec.RunnerImage = image
			if err := ValidateExecutionSpec(spec); err == nil {
				t.Fatalf("image %q unexpectedly accepted", image)
			}
		})
	}
}

func TestValidateAgentRunSpecResumeDigest(t *testing.T) {
	spec := agentrunv1alpha1.AgentRunSpec{Execution: validExecutionSpec()}
	if err := ValidateAgentRunSpec(spec); err != nil {
		t.Fatalf("empty approval rejected: %v", err)
	}
	spec.ResumeDecisionSHA256 = strings.Repeat("b", 64)
	if err := ValidateAgentRunSpec(spec); err != nil {
		t.Fatalf("valid approval rejected: %v", err)
	}
	spec.ResumeDecisionSHA256 = strings.Repeat("B", 64)
	if err := ValidateAgentRunSpec(spec); err == nil {
		t.Fatal("uppercase approval digest unexpectedly accepted")
	}
}
