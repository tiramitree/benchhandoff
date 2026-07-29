package controller

import (
	"context"
	"errors"
	"reflect"
	"time"

	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentrunv1alpha1 "github.com/tiramitree/benchhandoff/controller/api/v1alpha1"
)

const reconcilePollInterval = 2 * time.Second

// AgentRunReconciler turns one immutable execution spec into a bounded
// start/approval/resume/verify Job sequence. It never reads Pod logs or PVC
// bytes. Its only workload result input is the exact termination protocol.
type AgentRunReconciler struct {
	client.Client
	Scheme    *runtime.Scheme
	APIReader client.Reader
}

type statusSnapshotKey struct{}

// SetupWithManager registers the AgentRun and owned Job watches.
func (r *AgentRunReconciler) SetupWithManager(manager ctrl.Manager) error {
	if r.APIReader == nil {
		r.APIReader = manager.GetAPIReader()
	}
	return ctrl.NewControllerManagedBy(manager).
		For(&agentrunv1alpha1.AgentRun{}).
		Owns(&batchv1.Job{}).
		Complete(r)
}

// liveReader bypasses informer lag for fail-closed Job and Pod identity
// decisions. The cached client remains responsible for watches and writes.
func (r *AgentRunReconciler) liveReader() client.Reader {
	if r.APIReader != nil {
		return r.APIReader
	}
	return r.Client
}

// Reconcile advances at most one evidence-bound state transition.
func (r *AgentRunReconciler) Reconcile(
	ctx context.Context,
	request ctrl.Request,
) (ctrl.Result, error) {
	var run agentrunv1alpha1.AgentRun
	if err := r.Get(ctx, request.NamespacedName, &run); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}
	ctx = context.WithValue(
		ctx,
		statusSnapshotKey{},
		*run.Status.DeepCopy(),
	)

	specSHA, err := CanonicalExecutionSpecSHA(run.Spec.Execution)
	if err != nil {
		return r.block(ctx, &run, "InvalidExecutionSpec")
	}
	if err := ValidateAgentRunSpec(run.Spec); err != nil {
		return r.block(ctx, &run, "InvalidAgentRunSpec")
	}
	if run.Status.ExecutionSpecSHA256 != "" &&
		run.Status.ExecutionSpecSHA256 != specSHA {
		return r.block(ctx, &run, "ExecutionSpecChanged")
	}
	run.Status.ExecutionSpecSHA256 = specSHA

	phase := agentrunv1alpha1.AgentRunPhase(run.Status.Phase)
	if (phase == "" || phase == agentrunv1alpha1.PhasePending) &&
		run.Status.ActiveJobRef == nil &&
		run.Spec.ResumeDecisionSHA256 != "" {
		return r.block(ctx, &run, "PreseededApproval")
	}
	if phase == agentrunv1alpha1.PhaseSucceeded &&
		run.Spec.ResumeDecisionSHA256 != run.Status.ResumeDecisionSHA256 {
		return r.block(ctx, &run, "ApprovalBindingChanged")
	}

	switch phase {
	case agentrunv1alpha1.PhaseBlocked:
		return r.persistStatus(ctx, &run)
	case agentrunv1alpha1.PhaseSucceeded:
		setPhase(&run, agentrunv1alpha1.PhaseSucceeded, "Verified")
		return r.persistStatus(ctx, &run)
	case agentrunv1alpha1.PhaseAwaitingApproval:
		return r.reconcileApproval(ctx, &run, specSHA)
	}

	if run.Status.ActiveJobRef == nil {
		return r.ensureJob(ctx, &run, ActionStart, specSHA)
	}
	return r.reconcileActiveJob(ctx, &run, specSHA)
}

func (r *AgentRunReconciler) reconcileApproval(
	ctx context.Context,
	run *agentrunv1alpha1.AgentRun,
	specSHA string,
) (ctrl.Result, error) {
	if run.Status.ActiveJobRef == nil ||
		run.Status.ActiveJobRef.Action != string(ActionStart) {
		return r.block(ctx, run, "MissingStartJob")
	}
	startJob, result, err := r.loadBoundJob(ctx, run, ActionStart, specSHA)
	if err != nil {
		if errors.Is(err, errAmbiguousJobSet) {
			return r.block(ctx, run, "AmbiguousStartJob")
		}
		if errors.Is(err, errInvalidBoundJob) {
			return r.block(ctx, run, "StartJobBindingMismatch")
		}
		return result, err
	}
	if err := r.auditBoundPodSet(ctx, run, startJob, ActionStart); err != nil {
		if errors.Is(err, errAmbiguousPodSet) {
			return r.block(ctx, run, "AmbiguousPodSet")
		}
		return ctrl.Result{}, err
	}
	if run.Spec.ResumeDecisionSHA256 == "" {
		setPhase(run, agentrunv1alpha1.PhaseAwaitingApproval, "ApprovalRequired")
		return r.persistStatus(ctx, run)
	}
	if run.Spec.ResumeDecisionSHA256 != run.Status.ResumeDecisionSHA256 {
		return r.block(ctx, run, "ApprovalMismatch")
	}
	return r.ensureJob(ctx, run, ActionResume, specSHA)
}

func (r *AgentRunReconciler) reconcileActiveJob(
	ctx context.Context,
	run *agentrunv1alpha1.AgentRun,
	specSHA string,
) (ctrl.Result, error) {
	action := JobAction(run.Status.ActiveJobRef.Action)
	if !validAction(action) {
		return r.block(ctx, run, "InvalidActiveAction")
	}

	job, pending, err := r.loadBoundJob(ctx, run, action, specSHA)
	if err != nil {
		if errors.Is(err, errAmbiguousJobSet) {
			return r.block(ctx, run, "AmbiguousJobSet")
		}
		if errors.Is(err, errInvalidBoundJob) {
			return r.block(ctx, run, "JobBindingMismatch")
		}
		return pending, err
	}

	if err := r.auditBoundPodSet(ctx, run, job, action); err != nil {
		if errors.Is(err, errAmbiguousPodSet) {
			return r.block(ctx, run, "AmbiguousPodSet")
		}
		return ctrl.Result{}, err
	}

	if jobConditionTrue(job, batchv1.JobFailed) {
		return r.block(ctx, run, "JobFailed")
	}
	if !jobConditionTrue(job, batchv1.JobComplete) {
		if action == ActionVerify {
			setPhase(run, agentrunv1alpha1.PhaseVerifying, "VerifyRunning")
		} else {
			setPhase(run, agentrunv1alpha1.PhaseRunning, "RunnerActive")
		}
		statusResult, statusErr := r.persistStatus(ctx, run)
		if statusErr != nil {
			return statusResult, statusErr
		}
		return ctrl.Result{RequeueAfter: reconcilePollInterval}, nil
	}

	stepResult, result, err := r.readSingleStepResult(ctx, run, job, action, specSHA)
	if err != nil {
		if errors.Is(err, errAmbiguousPodSet) {
			return r.block(ctx, run, "AmbiguousPodSet")
		}
		return result, err
	}
	return r.acceptStepResult(ctx, run, specSHA, stepResult)
}

func (r *AgentRunReconciler) acceptStepResult(
	ctx context.Context,
	run *agentrunv1alpha1.AgentRun,
	specSHA string,
	result StepResult,
) (ctrl.Result, error) {
	switch result.Action {
	case ActionStart:
		switch result.Outcome {
		case OutcomeAwaitingApproval:
			run.Status.RunID = result.RunID
			run.Status.ResumeDecisionSHA256 = result.ResumeDecisionSHA256
			run.Status.BundleSHA256 = ""
			setPhase(
				run,
				agentrunv1alpha1.PhaseAwaitingApproval,
				"ApprovalRequired",
			)
			return r.persistStatus(ctx, run)
		case OutcomeCompleted:
			run.Status.RunID = result.RunID
			run.Status.BundleSHA256 = result.BundleSHA256
			return r.ensureJob(ctx, run, ActionVerify, specSHA)
		default:
			return r.block(ctx, run, "UnexpectedStartResult")
		}
	case ActionResume:
		if result.Outcome != OutcomeCompleted ||
			result.ResumeDecisionSHA256 != run.Spec.ResumeDecisionSHA256 ||
			result.RunID != run.Status.RunID {
			return r.block(ctx, run, "UnexpectedResumeResult")
		}
		run.Status.BundleSHA256 = result.BundleSHA256
		return r.ensureJob(ctx, run, ActionVerify, specSHA)
	case ActionVerify:
		if result.Outcome != OutcomeVerified ||
			result.RunID != run.Status.RunID ||
			result.BundleSHA256 != run.Status.BundleSHA256 {
			return r.block(ctx, run, "UnexpectedVerifyResult")
		}
		setPhase(run, agentrunv1alpha1.PhaseSucceeded, "Verified")
		return r.persistStatus(ctx, run)
	default:
		return r.block(ctx, run, "UnsupportedAction")
	}
}

var (
	errAmbiguousJobSet = errors.New("ambiguous Job set")
	errInvalidBoundJob = errors.New("invalid bound Job")
	errAmbiguousPodSet = errors.New("ambiguous Pod set")
)

func (r *AgentRunReconciler) ensureJob(
	ctx context.Context,
	run *agentrunv1alpha1.AgentRun,
	action JobAction,
	specSHA string,
) (ctrl.Result, error) {
	expected, err := BuildJob(run, string(action), specSHA)
	if err != nil {
		return r.block(ctx, run, "InvalidJobTemplate")
	}

	var jobs batchv1.JobList
	if err := r.liveReader().List(
		ctx,
		&jobs,
		client.InNamespace(run.Namespace),
		client.MatchingLabels{
			LabelRunUID: string(run.UID),
			LabelAction: string(action),
		},
	); err != nil {
		return ctrl.Result{}, err
	}
	if len(jobs.Items) > 1 {
		return r.block(ctx, run, "AmbiguousJobSet")
	}

	var job *batchv1.Job
	if len(jobs.Items) == 1 {
		job = &jobs.Items[0]
	} else {
		if err := r.Create(ctx, expected); err != nil {
			if !apierrors.IsAlreadyExists(err) {
				return ctrl.Result{}, err
			}
			var existing batchv1.Job
			if getErr := r.liveReader().Get(
				ctx,
				types.NamespacedName{
					Namespace: run.Namespace,
					Name:      expected.Name,
				},
				&existing,
			); getErr != nil {
				return ctrl.Result{}, getErr
			}
			job = &existing
		} else {
			job = expected
		}
	}
	if !agentRunUIDPattern.MatchString(string(job.UID)) {
		return r.block(ctx, run, "JobBindingMismatch")
	}
	if err := ValidateJob(job, run, string(action), specSHA); err != nil {
		return r.block(ctx, run, "JobBindingMismatch")
	}

	run.Status.ActiveJobRef = &agentrunv1alpha1.JobRef{
		Name:   job.Name,
		UID:    string(job.UID),
		Action: string(action),
	}
	if action == ActionVerify {
		setPhase(run, agentrunv1alpha1.PhaseVerifying, "VerifyScheduled")
	} else {
		setPhase(run, agentrunv1alpha1.PhaseRunning, "RunnerScheduled")
	}
	return r.persistStatus(ctx, run)
}

func (r *AgentRunReconciler) loadBoundJob(
	ctx context.Context,
	run *agentrunv1alpha1.AgentRun,
	action JobAction,
	specSHA string,
) (*batchv1.Job, ctrl.Result, error) {
	var jobs batchv1.JobList
	if err := r.liveReader().List(
		ctx,
		&jobs,
		client.InNamespace(run.Namespace),
		client.MatchingLabels{
			LabelRunUID: string(run.UID),
			LabelAction: string(action),
		},
	); err != nil {
		return nil, ctrl.Result{}, err
	}
	if len(jobs.Items) != 1 {
		if len(jobs.Items) > 1 {
			return nil, ctrl.Result{}, errAmbiguousJobSet
		}
		return nil, ctrl.Result{}, errInvalidBoundJob
	}
	job := &jobs.Items[0]
	if run.Status.ActiveJobRef == nil ||
		run.Status.ActiveJobRef.Name != job.Name ||
		run.Status.ActiveJobRef.UID != string(job.UID) ||
		run.Status.ActiveJobRef.Action != string(action) {
		return nil, ctrl.Result{}, errInvalidBoundJob
	}
	if err := ValidateJob(job, run, string(action), specSHA); err != nil {
		return nil, ctrl.Result{}, errInvalidBoundJob
	}
	return job, ctrl.Result{}, nil
}

// auditBoundPodSet rejects duplicate or foreign label-matching Pods even
// while the bound Job is still active. Zero Pods is a valid short-lived state
// immediately after Job creation.
func (r *AgentRunReconciler) auditBoundPodSet(
	ctx context.Context,
	run *agentrunv1alpha1.AgentRun,
	job *batchv1.Job,
	action JobAction,
) error {
	var pods corev1.PodList
	if err := r.liveReader().List(
		ctx,
		&pods,
		client.InNamespace(run.Namespace),
		client.MatchingLabels{
			LabelRunUID: string(run.UID),
			LabelAction: string(action),
		},
	); err != nil {
		return err
	}
	if len(pods.Items) > 1 ||
		(len(pods.Items) == 1 && !podOwnedByJob(&pods.Items[0], job)) {
		return errAmbiguousPodSet
	}
	return nil
}

func (r *AgentRunReconciler) readSingleStepResult(
	ctx context.Context,
	run *agentrunv1alpha1.AgentRun,
	job *batchv1.Job,
	action JobAction,
	specSHA string,
) (StepResult, ctrl.Result, error) {
	var pods corev1.PodList
	if err := r.liveReader().List(
		ctx,
		&pods,
		client.InNamespace(run.Namespace),
		client.MatchingLabels{
			LabelRunUID: string(run.UID),
			LabelAction: string(action),
		},
	); err != nil {
		return StepResult{}, ctrl.Result{}, err
	}
	if len(pods.Items) != 1 {
		return StepResult{}, ctrl.Result{}, errAmbiguousPodSet
	}
	pod := &pods.Items[0]
	if !podOwnedByJob(pod, job) {
		return StepResult{}, ctrl.Result{}, errAmbiguousPodSet
	}

	message := ""
	for _, status := range pod.Status.ContainerStatuses {
		if status.Name == RunnerContainerName && status.State.Terminated != nil {
			message = status.State.Terminated.Message
			break
		}
	}
	if message == "" {
		return StepResult{}, ctrl.Result{}, errAmbiguousPodSet
	}
	stepResult, err := ParseTerminationMessage(message)
	if err != nil {
		return StepResult{}, ctrl.Result{}, errAmbiguousPodSet
	}
	if err := ValidateStepResult(
		stepResult,
		string(run.UID),
		string(action),
		specSHA,
	); err != nil {
		return StepResult{}, ctrl.Result{}, errAmbiguousPodSet
	}
	return stepResult, ctrl.Result{}, nil
}

func podOwnedByJob(pod *corev1.Pod, job *batchv1.Job) bool {
	for _, owner := range pod.OwnerReferences {
		if owner.Controller != nil && *owner.Controller &&
			owner.Kind == "Job" &&
			owner.APIVersion == batchv1.SchemeGroupVersion.String() &&
			owner.Name == job.Name &&
			owner.UID == job.UID {
			return true
		}
	}
	return false
}

func jobConditionTrue(job *batchv1.Job, conditionType batchv1.JobConditionType) bool {
	for _, condition := range job.Status.Conditions {
		if condition.Type == conditionType && condition.Status == corev1.ConditionTrue {
			return true
		}
	}
	return false
}

func (r *AgentRunReconciler) block(
	ctx context.Context,
	run *agentrunv1alpha1.AgentRun,
	reason string,
) (ctrl.Result, error) {
	setPhase(run, agentrunv1alpha1.PhaseBlocked, reason)
	return r.persistStatus(ctx, run)
}

func setPhase(
	run *agentrunv1alpha1.AgentRun,
	phase agentrunv1alpha1.AgentRunPhase,
	reason string,
) {
	run.Status.Phase = string(phase)
	conditionStatus := metav1.ConditionUnknown
	message := "The controller is reconciling one bounded Job."
	switch phase {
	case agentrunv1alpha1.PhaseAwaitingApproval:
		message = "Resume is paused until the spec contains the exact observed decision digest."
	case agentrunv1alpha1.PhaseSucceeded:
		conditionStatus = metav1.ConditionTrue
		message = "A distinct verification Job accepted the bound evidence bundle."
	case agentrunv1alpha1.PhaseBlocked:
		conditionStatus = metav1.ConditionFalse
		message = "The controller stopped at a registered fail-closed boundary."
	}
	meta.SetStatusCondition(&run.Status.Conditions, metav1.Condition{
		Type:               "Ready",
		Status:             conditionStatus,
		ObservedGeneration: run.Generation,
		Reason:             reason,
		Message:            message,
	})
}

func (r *AgentRunReconciler) persistStatus(
	ctx context.Context,
	run *agentrunv1alpha1.AgentRun,
) (ctrl.Result, error) {
	run.Status.ObservedGeneration = run.Generation
	original, hasOriginal := ctx.Value(statusSnapshotKey{}).(agentrunv1alpha1.AgentRunStatus)
	if hasOriginal && reflect.DeepEqual(original, run.Status) {
		return ctrl.Result{}, nil
	}
	if err := r.Status().Update(ctx, run); err != nil {
		return ctrl.Result{}, err
	}
	return ctrl.Result{}, nil
}
