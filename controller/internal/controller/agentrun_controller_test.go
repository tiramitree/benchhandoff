package controller

import (
	"context"
	"strings"
	"testing"

	agentrunv1alpha1 "github.com/tiramitree/benchhandoff/controller/api/v1alpha1"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
)

const (
	testJobUID        = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
	testCreatedJobUID = "cccccccc-dddd-4eee-8fff-aaaaaaaaaaaa"
	testRunID         = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
)

func controllerTestScheme(t *testing.T) *runtime.Scheme {
	t.Helper()
	scheme := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(scheme); err != nil {
		t.Fatalf("add client-go scheme: %v", err)
	}
	if err := agentrunv1alpha1.AddToScheme(scheme); err != nil {
		t.Fatalf("add AgentRun scheme: %v", err)
	}
	return scheme
}

func controllerTestClient(
	t *testing.T,
	scheme *runtime.Scheme,
	objects ...client.Object,
) client.Client {
	t.Helper()
	return fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(&agentrunv1alpha1.AgentRun{}).
		WithObjects(objects...).
		Build()
}

type jobUIDAssigningClient struct {
	client.Client
	uid types.UID
}

func (c *jobUIDAssigningClient) Create(
	ctx context.Context,
	object client.Object,
	options ...client.CreateOption,
) error {
	if job, ok := object.(*batchv1.Job); ok && job.UID == "" {
		job.UID = c.uid
	}
	return c.Client.Create(ctx, object, options...)
}

func controllerTestRequest(run *agentrunv1alpha1.AgentRun) ctrl.Request {
	return ctrl.Request{
		NamespacedName: types.NamespacedName{
			Namespace: run.Namespace,
			Name:      run.Name,
		},
	}
}

func reconcileOnce(
	t *testing.T,
	reconciler *AgentRunReconciler,
	run *agentrunv1alpha1.AgentRun,
) {
	t.Helper()
	if _, err := reconciler.Reconcile(
		context.Background(),
		controllerTestRequest(run),
	); err != nil {
		t.Fatalf("Reconcile: %v", err)
	}
}

func getControllerTestRun(
	t *testing.T,
	cached client.Client,
	run *agentrunv1alpha1.AgentRun,
) *agentrunv1alpha1.AgentRun {
	t.Helper()
	var current agentrunv1alpha1.AgentRun
	if err := cached.Get(
		context.Background(),
		client.ObjectKeyFromObject(run),
		&current,
	); err != nil {
		t.Fatalf("get AgentRun: %v", err)
	}
	return &current
}

func assertControllerPhaseReason(
	t *testing.T,
	run *agentrunv1alpha1.AgentRun,
	phase agentrunv1alpha1.AgentRunPhase,
	reason string,
) {
	t.Helper()
	if run.Status.Phase != string(phase) {
		t.Fatalf("phase = %q, want %q", run.Status.Phase, phase)
	}
	for _, condition := range run.Status.Conditions {
		if condition.Type == "Ready" {
			if condition.Reason != reason {
				t.Fatalf("Ready reason = %q, want %q", condition.Reason, reason)
			}
			return
		}
	}
	t.Fatal("Ready condition is missing")
}

func listControllerTestJobs(
	t *testing.T,
	reader client.Reader,
	run *agentrunv1alpha1.AgentRun,
	action JobAction,
) []batchv1.Job {
	t.Helper()
	var jobs batchv1.JobList
	if err := reader.List(
		context.Background(),
		&jobs,
		client.InNamespace(run.Namespace),
		client.MatchingLabels{
			LabelRunUID: string(run.UID),
			LabelAction: string(action),
		},
	); err != nil {
		t.Fatalf("list %s Jobs: %v", action, err)
	}
	return jobs.Items
}

func bindControllerTestJob(
	t *testing.T,
	run *agentrunv1alpha1.AgentRun,
	action JobAction,
) (*batchv1.Job, string) {
	t.Helper()
	job, specSHA := buildValidJob(t, run, action)
	job.UID = types.UID(testJobUID)
	run.Status.ExecutionSpecSHA256 = specSHA
	run.Status.ActiveJobRef = &agentrunv1alpha1.JobRef{
		Name:   job.Name,
		UID:    string(job.UID),
		Action: string(action),
	}
	return job, specSHA
}

func TestReconcilePreseededApprovalBlocksWithoutCreatingJob(t *testing.T) {
	scheme := controllerTestScheme(t)
	run := validAgentRun()
	run.Generation = 1
	run.Spec.ResumeDecisionSHA256 = strings.Repeat("d", 64)
	cached := controllerTestClient(t, scheme, run)
	reconciler := &AgentRunReconciler{
		Client:    cached,
		Scheme:    scheme,
		APIReader: cached,
	}

	reconcileOnce(t, reconciler, run)

	current := getControllerTestRun(t, cached, run)
	assertControllerPhaseReason(
		t,
		current,
		agentrunv1alpha1.PhaseBlocked,
		"PreseededApproval",
	)
	if jobs := listControllerTestJobs(t, cached, run, ActionStart); len(jobs) != 0 {
		t.Fatalf("preseeded approval created %d start Jobs", len(jobs))
	}
	if jobs := listControllerTestJobs(t, cached, run, ActionResume); len(jobs) != 0 {
		t.Fatalf("preseeded approval created %d resume Jobs", len(jobs))
	}
}

func TestReconcileExactApprovalCreatesOneResumeJob(t *testing.T) {
	scheme := controllerTestScheme(t)
	run := validAgentRun()
	run.Generation = 2
	decision := strings.Repeat("d", 64)
	run.Spec.ResumeDecisionSHA256 = decision
	run.Status.Phase = string(agentrunv1alpha1.PhaseAwaitingApproval)
	run.Status.RunID = testRunID
	run.Status.ResumeDecisionSHA256 = decision
	startJob, _ := bindControllerTestJob(t, run, ActionStart)
	cached := controllerTestClient(t, scheme, run, startJob)
	writer := &jobUIDAssigningClient{
		Client: cached,
		uid:    types.UID(testCreatedJobUID),
	}
	reconciler := &AgentRunReconciler{
		Client:    writer,
		Scheme:    scheme,
		APIReader: cached,
	}

	reconcileOnce(t, reconciler, run)
	reconcileOnce(t, reconciler, run)

	current := getControllerTestRun(t, cached, run)
	assertControllerPhaseReason(
		t,
		current,
		agentrunv1alpha1.PhaseRunning,
		"RunnerActive",
	)
	if current.Status.ActiveJobRef == nil ||
		current.Status.ActiveJobRef.Action != string(ActionResume) {
		t.Fatalf("active Job ref = %#v, want resume", current.Status.ActiveJobRef)
	}
	resumeJobs := listControllerTestJobs(t, cached, run, ActionResume)
	if len(resumeJobs) != 1 {
		t.Fatalf("resume Job count = %d, want 1", len(resumeJobs))
	}
	expected, specSHA := buildValidJob(t, run, ActionResume)
	if resumeJobs[0].Name != expected.Name {
		t.Fatalf("resume Job name = %q, want %q", resumeJobs[0].Name, expected.Name)
	}
	if current.Status.ActiveJobRef.UID != testCreatedJobUID {
		t.Fatalf("resume Job UID = %q, want %q", current.Status.ActiveJobRef.UID, testCreatedJobUID)
	}
	if err := ValidateJob(&resumeJobs[0], run, "resume", specSHA); err != nil {
		t.Fatalf("created resume Job is not bound: %v", err)
	}
}

func TestReconcileDuplicateJobsFailsClosed(t *testing.T) {
	scheme := controllerTestScheme(t)
	run := validAgentRun()
	run.Generation = 3
	run.Status.Phase = string(agentrunv1alpha1.PhaseRunning)
	first, _ := bindControllerTestJob(t, run, ActionStart)
	second := first.DeepCopy()
	second.Name += "-duplicate"
	second.UID = "ffffffff-eeee-4ddd-8ccc-bbbbbbbbbbbb"
	cached := controllerTestClient(t, scheme, run, first, second)
	reconciler := &AgentRunReconciler{
		Client:    cached,
		Scheme:    scheme,
		APIReader: cached,
	}

	reconcileOnce(t, reconciler, run)

	current := getControllerTestRun(t, cached, run)
	assertControllerPhaseReason(
		t,
		current,
		agentrunv1alpha1.PhaseBlocked,
		"AmbiguousJobSet",
	)
}

func TestReconcileDuplicatePodsFailsClosed(t *testing.T) {
	scheme := controllerTestScheme(t)
	run := validAgentRun()
	run.Generation = 4
	run.Status.Phase = string(agentrunv1alpha1.PhaseRunning)
	job, _ := bindControllerTestJob(t, run, ActionStart)
	controller := true
	newPod := func(name string) *corev1.Pod {
		return &corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{
				Name:      name,
				Namespace: run.Namespace,
				Labels: map[string]string{
					LabelRunUID: string(run.UID),
					LabelAction: string(ActionStart),
				},
				OwnerReferences: []metav1.OwnerReference{
					{
						APIVersion: batchv1.SchemeGroupVersion.String(),
						Kind:       "Job",
						Name:       job.Name,
						UID:        job.UID,
						Controller: &controller,
					},
				},
			},
		}
	}
	firstPod := newPod("runner-one")
	secondPod := newPod("runner-two")
	cached := controllerTestClient(t, scheme, run, job, firstPod, secondPod)
	reconciler := &AgentRunReconciler{
		Client:    cached,
		Scheme:    scheme,
		APIReader: cached,
	}

	reconcileOnce(t, reconciler, run)

	current := getControllerTestRun(t, cached, run)
	assertControllerPhaseReason(
		t,
		current,
		agentrunv1alpha1.PhaseBlocked,
		"AmbiguousPodSet",
	)
}

func TestReconcileAwaitingApprovalStillAuditsDuplicateStartPods(t *testing.T) {
	scheme := controllerTestScheme(t)
	run := validAgentRun()
	run.Generation = 5
	run.Status.Phase = string(agentrunv1alpha1.PhaseAwaitingApproval)
	run.Status.ResumeDecisionSHA256 = strings.Repeat("d", 64)
	job, _ := bindControllerTestJob(t, run, ActionStart)
	controller := true
	newPod := func(name string) *corev1.Pod {
		return &corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{
				Name:      name,
				Namespace: run.Namespace,
				Labels: map[string]string{
					LabelRunUID: string(run.UID),
					LabelAction: string(ActionStart),
				},
				OwnerReferences: []metav1.OwnerReference{
					{
						APIVersion: batchv1.SchemeGroupVersion.String(),
						Kind:       "Job",
						Name:       job.Name,
						UID:        job.UID,
						Controller: &controller,
					},
				},
			},
		}
	}
	firstPod := newPod("waiting-runner-one")
	secondPod := newPod("waiting-runner-two")
	cached := controllerTestClient(t, scheme, run, job, firstPod, secondPod)
	reconciler := &AgentRunReconciler{
		Client:    cached,
		Scheme:    scheme,
		APIReader: cached,
	}

	reconcileOnce(t, reconciler, run)

	current := getControllerTestRun(t, cached, run)
	assertControllerPhaseReason(
		t,
		current,
		agentrunv1alpha1.PhaseBlocked,
		"AmbiguousPodSet",
	)
	if jobs := listControllerTestJobs(t, cached, run, ActionResume); len(jobs) != 0 {
		t.Fatalf("ambiguous start Pods created %d resume Jobs", len(jobs))
	}
}

func TestReconcileSucceededApprovalBindingChangeFailsClosed(t *testing.T) {
	scheme := controllerTestScheme(t)
	run := validAgentRun()
	run.Generation = 5
	run.Status.Phase = string(agentrunv1alpha1.PhaseSucceeded)
	run.Status.ResumeDecisionSHA256 = strings.Repeat("d", 64)
	run.Spec.ResumeDecisionSHA256 = strings.Repeat("e", 64)
	cached := controllerTestClient(t, scheme, run)
	reconciler := &AgentRunReconciler{
		Client:    cached,
		Scheme:    scheme,
		APIReader: cached,
	}

	reconcileOnce(t, reconciler, run)

	current := getControllerTestRun(t, cached, run)
	assertControllerPhaseReason(
		t,
		current,
		agentrunv1alpha1.PhaseBlocked,
		"ApprovalBindingChanged",
	)
}

func TestReconcileUsesFreshAPIReaderWhenCachedClientMissesJob(t *testing.T) {
	scheme := controllerTestScheme(t)
	run := validAgentRun()
	run.Generation = 6
	job, _ := buildValidJob(t, run, ActionStart)
	job.UID = types.UID(testJobUID)

	cached := controllerTestClient(t, scheme, run)
	live := controllerTestClient(t, scheme, job)
	reconciler := &AgentRunReconciler{
		Client:    cached,
		Scheme:    scheme,
		APIReader: live,
	}

	reconcileOnce(t, reconciler, run)

	current := getControllerTestRun(t, cached, run)
	assertControllerPhaseReason(
		t,
		current,
		agentrunv1alpha1.PhaseRunning,
		"RunnerScheduled",
	)
	if current.Status.ActiveJobRef == nil ||
		current.Status.ActiveJobRef.Name != job.Name ||
		current.Status.ActiveJobRef.UID != string(job.UID) {
		t.Fatalf("controller did not bind the live Job: %#v", current.Status.ActiveJobRef)
	}
	if jobs := listControllerTestJobs(t, cached, run, ActionStart); len(jobs) != 0 {
		t.Fatalf("cached client unexpectedly received %d created Jobs", len(jobs))
	}
	if jobs := listControllerTestJobs(t, live, run, ActionStart); len(jobs) != 1 {
		t.Fatalf("live APIReader Job count = %d, want 1", len(jobs))
	}
}
