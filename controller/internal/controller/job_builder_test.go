package controller

import (
	"reflect"
	"strings"
	"testing"

	agentrunv1alpha1 "github.com/tiramitree/benchhandoff/controller/api/v1alpha1"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
)

func validAgentRun() *agentrunv1alpha1.AgentRun {
	return &agentrunv1alpha1.AgentRun{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "sample-run",
			Namespace: "benchhandoff-system",
			UID:       types.UID(testAgentRunUID),
		},
		Spec: agentrunv1alpha1.AgentRunSpec{
			Execution: validExecutionSpec(),
		},
	}
}

func buildValidJob(t *testing.T, run *agentrunv1alpha1.AgentRun, action JobAction) (*batchv1.Job, string) {
	t.Helper()
	specSHA, err := CanonicalExecutionSpecSHA(run.Spec.Execution)
	if err != nil {
		t.Fatalf("CanonicalExecutionSpecSHA: %v", err)
	}
	job, err := BuildJob(run, string(action), specSHA)
	if err != nil {
		t.Fatalf("BuildJob: %v", err)
	}
	return job, specSHA
}

func TestBuildJobStartTemplate(t *testing.T) {
	run := validAgentRun()
	job, specSHA := buildValidJob(t, run, ActionStart)

	if job.Name != "agentrun-start-928f2b91ec124879" {
		t.Fatalf("deterministic Job name = %q", job.Name)
	}
	if job.Namespace != run.Namespace {
		t.Fatalf("namespace = %q", job.Namespace)
	}
	if !reflect.DeepEqual(job.Labels, map[string]string{
		LabelRunUID: testAgentRunUID,
		LabelAction: string(ActionStart),
	}) {
		t.Fatalf("unexpected Job labels: %#v", job.Labels)
	}
	if job.Annotations[AnnotationExecutionSpecSHA256] != specSHA ||
		job.Annotations[AnnotationRunUID] != testAgentRunUID ||
		job.Annotations[AnnotationAction] != string(ActionStart) {
		t.Fatalf("unexpected Job annotations: %#v", job.Annotations)
	}
	if len(job.OwnerReferences) != 1 ||
		job.OwnerReferences[0].UID != run.UID ||
		job.OwnerReferences[0].Kind != "AgentRun" ||
		job.OwnerReferences[0].Controller == nil ||
		!*job.OwnerReferences[0].Controller ||
		job.OwnerReferences[0].BlockOwnerDeletion != nil {
		t.Fatalf("unexpected owner reference: %#v", job.OwnerReferences)
	}

	if job.Spec.BackoffLimit == nil || *job.Spec.BackoffLimit != 0 ||
		job.Spec.Parallelism == nil || *job.Spec.Parallelism != 1 ||
		job.Spec.Completions == nil || *job.Spec.Completions != 1 ||
		job.Spec.ActiveDeadlineSeconds == nil ||
		*job.Spec.ActiveDeadlineSeconds != run.Spec.Execution.ActiveDeadlineSeconds {
		t.Fatalf("unexpected bounded Job settings: %#v", job.Spec)
	}
	if job.Spec.PodReplacementPolicy == nil ||
		*job.Spec.PodReplacementPolicy != batchv1.TerminatingOrFailed {
		t.Fatalf("unexpected Pod replacement policy: %#v", job.Spec.PodReplacementPolicy)
	}
	pod := job.Spec.Template.Spec
	if len(pod.Containers) != 1 || pod.Containers[0].Name != RunnerContainerName {
		t.Fatalf("unexpected containers: %#v", pod.Containers)
	}
	container := pod.Containers[0]
	if len(container.Command) != 0 {
		t.Fatalf("Job must preserve the digest-bound image ENTRYPOINT, command = %#v", container.Command)
	}
	wantArgs := []string{
		"--action", "start",
		"--agent-run-uid", testAgentRunUID,
		"--execution-spec-sha256", specSHA,
		"--suite-path", "demo/suite.toml",
		"--suite-sha256", strings.Repeat("b", 64),
	}
	if !reflect.DeepEqual(container.Args, wantArgs) {
		t.Fatalf("args = %#v, want %#v", container.Args, wantArgs)
	}
	if container.WorkingDir != DataRoot ||
		container.TerminationMessagePath != "/dev/termination-log" {
		t.Fatalf("unexpected working or termination path: %#v", container)
	}
	env := map[string]string{}
	for _, variable := range container.Env {
		env[variable.Name] = variable.Value
	}
	if len(env) != 1 || env["TMPDIR"] != "/tmp" {
		t.Fatalf("unexpected environment: %#v", env)
	}
	if _, ok := env["PYTHONUNBUFFERED"]; ok {
		t.Fatal("isolated Python must use explicit -u rather than ignored environment")
	}
	if _, ok := env["PYTHONDONTWRITEBYTECODE"]; ok {
		t.Fatal("isolated Python must use explicit -B rather than ignored environment")
	}
	if _, ok := env["HOME"]; ok {
		t.Fatal("Job must not override HOME")
	}
	if _, ok := env["PYTHONPATH"]; ok {
		t.Fatal("Job must not override the runner image PYTHONPATH")
	}
	if pod.SecurityContext == nil ||
		pod.SecurityContext.RunAsUser == nil || *pod.SecurityContext.RunAsUser != 65532 ||
		pod.SecurityContext.RunAsGroup == nil || *pod.SecurityContext.RunAsGroup != 65532 {
		t.Fatalf("unexpected Pod security context: %#v", pod.SecurityContext)
	}
	if pod.SecurityContext.FSGroup != nil ||
		pod.SecurityContext.FSGroupChangePolicy != nil {
		t.Fatalf(
			"Job must not request recursive PVC ownership changes: %#v",
			pod.SecurityContext,
		)
	}
	if container.SecurityContext == nil ||
		container.SecurityContext.RunAsUser == nil ||
		*container.SecurityContext.RunAsUser != 65532 ||
		container.SecurityContext.RunAsGroup == nil ||
		*container.SecurityContext.RunAsGroup != 65532 {
		t.Fatalf("unexpected container security context: %#v", container.SecurityContext)
	}
	if len(pod.Volumes) != 2 ||
		pod.Volumes[0].PersistentVolumeClaim == nil ||
		pod.Volumes[0].PersistentVolumeClaim.ClaimName != run.Spec.Execution.PVCName {
		t.Fatalf("unexpected volumes: %#v", pod.Volumes)
	}
	if pod.PreemptionPolicy == nil ||
		*pod.PreemptionPolicy != corev1.PreemptLowerPriority {
		t.Fatalf("unexpected preemption policy: %#v", pod.PreemptionPolicy)
	}
}

func TestBuildJobResumeBindsApproval(t *testing.T) {
	run := validAgentRun()
	run.Spec.ResumeDecisionSHA256 = strings.Repeat("d", 64)
	job, _ := buildValidJob(t, run, ActionResume)
	args := job.Spec.Template.Spec.Containers[0].Args
	wantTail := []string{
		"--resume-decision-sha256",
		run.Spec.ResumeDecisionSHA256,
	}
	if !reflect.DeepEqual(args[len(args)-2:], wantTail) {
		t.Fatalf("resume args tail = %#v", args[len(args)-2:])
	}
}

func TestBuildJobAllActionsBindSuiteDigest(t *testing.T) {
	for _, action := range []JobAction{ActionStart, ActionResume, ActionVerify} {
		t.Run(string(action), func(t *testing.T) {
			run := validAgentRun()
			if action == ActionResume {
				run.Spec.ResumeDecisionSHA256 = strings.Repeat("d", 64)
			}
			job, _ := buildValidJob(t, run, action)
			args := job.Spec.Template.Spec.Containers[0].Args
			found := 0
			for index := 0; index < len(args); index++ {
				if args[index] != "--suite-sha256" {
					continue
				}
				found++
				if index+1 >= len(args) {
					t.Fatal("--suite-sha256 has no value")
				}
				if args[index+1] != run.Spec.Execution.SuiteSHA256 {
					t.Fatalf(
						"suite digest arg = %q, want %q",
						args[index+1],
						run.Spec.Execution.SuiteSHA256,
					)
				}
			}
			if found != 1 {
				t.Fatalf("--suite-sha256 occurrence count = %d, want 1", found)
			}
		})
	}
}

func TestBuildJobRejectsInvalidBindings(t *testing.T) {
	run := validAgentRun()
	specSHA, err := CanonicalExecutionSpecSHA(run.Spec.Execution)
	if err != nil {
		t.Fatal(err)
	}
	tests := []struct {
		name   string
		mutate func(*agentrunv1alpha1.AgentRun)
		action string
		hash   string
	}{
		{"wrong hash", func(*agentrunv1alpha1.AgentRun) {}, "start", strings.Repeat("f", 64)},
		{"bad UID", func(run *agentrunv1alpha1.AgentRun) { run.UID = "not-a-uuid" }, "start", specSHA},
		{"bad namespace", func(run *agentrunv1alpha1.AgentRun) { run.Namespace = "" }, "start", specSHA},
		{"unknown action", func(*agentrunv1alpha1.AgentRun) {}, "delete", specSHA},
		{"resume without approval", func(*agentrunv1alpha1.AgentRun) {}, "resume", specSHA},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			candidate := run.DeepCopy()
			test.mutate(candidate)
			if _, err := BuildJob(candidate, test.action, test.hash); err == nil {
				t.Fatal("invalid Job binding unexpectedly accepted")
			}
		})
	}
}

func TestHistoricalStartJobStillValidAfterApproval(t *testing.T) {
	run := validAgentRun()
	job, specSHA := buildValidJob(t, run, ActionStart)
	run.Spec.ResumeDecisionSHA256 = strings.Repeat("d", 64)

	rebuilt, err := BuildJob(run, "start", specSHA)
	if err != nil {
		t.Fatalf("rebuild historical start after approval: %v", err)
	}
	if !reflect.DeepEqual(job, rebuilt) {
		t.Fatal("approval changed the historical start Job template")
	}
	if err := ValidateJob(job, run, "start", specSHA); err != nil {
		t.Fatalf("historical start rejected after exact approval: %v", err)
	}
}

func TestValidateJobAllowsOnlyPinnedKubernetes136DefaultsAndGeneratedLabels(t *testing.T) {
	run := validAgentRun()
	job, specSHA := buildValidJob(t, run, ActionStart)
	if err := ValidateJob(job, run, "start", specSHA); err != nil {
		t.Fatalf("fresh template rejected: %v", err)
	}

	// Kubernetes v1.36.1 defaults this field on Job admission. BuildJob
	// materializes it so the stored object remains exactly comparable without
	// pretending that the client-go scheme runs API-server defaulting.
	defaulted := job.DeepCopy()
	defaulted.Spec.PodReplacementPolicy = nil
	if defaulted.Spec.PodReplacementPolicy == nil {
		policy := batchv1.TerminatingOrFailed
		defaulted.Spec.PodReplacementPolicy = &policy
	}
	if err := ValidateJob(defaulted, run, "start", specSHA); err != nil {
		t.Fatalf("pinned Kubernetes v1.36.1 defaults rejected: %v", err)
	}

	wrongPolicy := defaulted.DeepCopy()
	failed := batchv1.Failed
	wrongPolicy.Spec.PodReplacementPolicy = &failed
	if err := ValidateJob(wrongPolicy, run, "start", specSHA); err == nil {
		t.Fatal("unexpected Pod replacement policy was accepted")
	}

	live := defaulted.DeepCopy()
	live.UID = types.UID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
	controllerUID := string(live.UID)
	live.Spec.Selector = &metav1.LabelSelector{MatchLabels: map[string]string{
		"batch.kubernetes.io/controller-uid": controllerUID,
		"controller-uid":                     controllerUID,
	}}
	live.Spec.Template.Labels["batch.kubernetes.io/controller-uid"] = controllerUID
	live.Spec.Template.Labels["controller-uid"] = controllerUID
	live.Spec.Template.Labels["batch.kubernetes.io/job-name"] = live.Name
	live.Spec.Template.Labels["job-name"] = live.Name
	if err := ValidateJob(live, run, "start", specSHA); err != nil {
		t.Fatalf("known API-server labels rejected: %v", err)
	}

	mutations := []struct {
		name   string
		mutate func(*batchv1.Job)
	}{
		{
			"runner image",
			func(job *batchv1.Job) {
				job.Spec.Template.Spec.Containers[0].Image = "example.invalid/other@sha256:" + strings.Repeat("f", 64)
			},
		},
		{
			"argv",
			func(job *batchv1.Job) {
				job.Spec.Template.Spec.Containers[0].Args[1] = "verify"
			},
		},
		{
			"full spec annotation",
			func(job *batchv1.Job) {
				job.Annotations[AnnotationExecutionSpecSHA256] = strings.Repeat("f", 64)
			},
		},
		{
			"extra Job label",
			func(job *batchv1.Job) {
				job.Labels["unexpected"] = "value"
			},
		},
		{
			"extra Pod template label",
			func(job *batchv1.Job) {
				job.Spec.Template.Labels["unexpected"] = "value"
			},
		},
		{
			"unexpected selector",
			func(job *batchv1.Job) {
				job.Spec.Selector.MatchLabels["unexpected"] = "value"
			},
		},
	}
	for _, mutation := range mutations {
		t.Run(mutation.name, func(t *testing.T) {
			candidate := live.DeepCopy()
			mutation.mutate(candidate)
			if err := ValidateJob(candidate, run, "start", specSHA); err == nil {
				t.Fatal("mutated Job unexpectedly accepted")
			}
		})
	}
}
