package controller

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"reflect"

	agentrunv1alpha1 "github.com/tiramitree/benchhandoff/controller/api/v1alpha1"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/validation"
)

const (
	LabelRunUID                   = "control.benchhandoff.dev/run-uid"
	LabelAction                   = "control.benchhandoff.dev/action"
	AnnotationRunUID              = "control.benchhandoff.dev/run-uid"
	AnnotationAction              = "control.benchhandoff.dev/action"
	AnnotationExecutionSpecSHA256 = "control.benchhandoff.dev/execution-spec-sha256"
	RunnerContainerName           = "runner"
	DataVolumeName                = "benchhandoff-data"
	TemporaryVolumeName           = "tmp"
	TemporaryMountPath            = "/tmp"
)

var dropAllCapabilities = []corev1.Capability{"ALL"}

// BuildJob constructs the only accepted runner Job template. It binds the
// owner, action, and canonical execution spec in immutable argv and metadata.
func BuildJob(run *agentrunv1alpha1.AgentRun, action, specSHA string) (*batchv1.Job, error) {
	if run == nil {
		return nil, errors.New("AgentRun is required")
	}
	if run.Namespace == "" || len(validation.IsDNS1123Label(run.Namespace)) != 0 {
		return nil, errors.New("AgentRun namespace must be a valid DNS-1123 label")
	}
	if run.Name == "" || len(validation.IsDNS1123Subdomain(run.Name)) != 0 {
		return nil, errors.New("AgentRun name must be a valid DNS-1123 subdomain")
	}
	runUID := string(run.UID)
	if !agentRunUIDPattern.MatchString(runUID) {
		return nil, errors.New("AgentRun UID must be a lowercase UUID")
	}
	jobAction := JobAction(action)
	if !validAction(jobAction) {
		return nil, fmt.Errorf("unsupported Job action %q", action)
	}
	if err := ValidateAgentRunSpec(run.Spec); err != nil {
		return nil, fmt.Errorf("invalid AgentRun spec: %w", err)
	}
	canonicalSHA, err := CanonicalExecutionSpecSHA(run.Spec.Execution)
	if err != nil {
		return nil, err
	}
	if specSHA != canonicalSHA {
		return nil, errors.New("execution spec SHA-256 does not match the canonical AgentRun spec")
	}
	switch jobAction {
	case ActionResume:
		if !IsSHA256(run.Spec.ResumeDecisionSHA256) {
			return nil, errors.New("resume action requires an exact resume-decision SHA-256")
		}
	}

	labels := map[string]string{
		LabelRunUID: runUID,
		LabelAction: action,
	}
	annotations := map[string]string{
		AnnotationRunUID:              runUID,
		AnnotationAction:              action,
		AnnotationExecutionSpecSHA256: specSHA,
	}
	arguments := []string{
		"--action", action,
		"--agent-run-uid", runUID,
		"--execution-spec-sha256", specSHA,
		"--suite-path", run.Spec.Execution.SuitePath,
		"--suite-sha256", run.Spec.Execution.SuiteSHA256,
	}
	if jobAction == ActionResume {
		arguments = append(
			arguments,
			"--resume-decision-sha256", run.Spec.ResumeDecisionSHA256,
		)
	}

	backoffLimit := int32(0)
	completions := int32(1)
	parallelism := int32(1)
	deadline := run.Spec.Execution.ActiveDeadlineSeconds
	manualSelector := false
	suspend := false
	automountToken := false
	enableServiceLinks := false
	runAsNonRoot := true
	allowPrivilegeEscalation := false
	readOnlyRootFilesystem := true
	terminationGracePeriod := int64(30)
	completionMode := batchv1.NonIndexedCompletion
	runAsUser := int64(65532)
	podReplacementPolicy := batchv1.TerminatingOrFailed
	runAsGroup := int64(65532)
	preemptionPolicy := corev1.PreemptLowerPriority
	controllerOwner := true

	return &batchv1.Job{
		TypeMeta: metav1.TypeMeta{
			APIVersion: batchv1.SchemeGroupVersion.String(),
			Kind:       "Job",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name:        deterministicJobName(runUID, jobAction, specSHA),
			Namespace:   run.Namespace,
			Labels:      copyStringMap(labels),
			Annotations: copyStringMap(annotations),
			OwnerReferences: []metav1.OwnerReference{
				{
					APIVersion: agentrunv1alpha1.GroupVersion.String(),
					Kind:       "AgentRun",
					Name:       run.Name,
					UID:        run.UID,
					Controller: &controllerOwner,
				},
			},
		},
		Spec: batchv1.JobSpec{
			Parallelism:           &parallelism,
			Completions:           &completions,
			ActiveDeadlineSeconds: &deadline,
			BackoffLimit:          &backoffLimit,
			ManualSelector:        &manualSelector,
			CompletionMode:        &completionMode,
			Suspend:               &suspend,
			PodReplacementPolicy:  &podReplacementPolicy,
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{
					Labels:      copyStringMap(labels),
					Annotations: copyStringMap(annotations),
				},
				Spec: corev1.PodSpec{
					AutomountServiceAccountToken:  &automountToken,
					EnableServiceLinks:            &enableServiceLinks,
					RestartPolicy:                 corev1.RestartPolicyNever,
					DNSPolicy:                     corev1.DNSClusterFirst,
					SchedulerName:                 corev1.DefaultSchedulerName,
					TerminationGracePeriodSeconds: &terminationGracePeriod,
					PreemptionPolicy:              &preemptionPolicy,
					SecurityContext: &corev1.PodSecurityContext{
						RunAsNonRoot: &runAsNonRoot,
						RunAsUser:    &runAsUser,
						RunAsGroup:   &runAsGroup,
						SeccompProfile: &corev1.SeccompProfile{
							Type: corev1.SeccompProfileTypeRuntimeDefault,
						},
					},
					Containers: []corev1.Container{
						{
							Name:            RunnerContainerName,
							Image:           run.Spec.Execution.RunnerImage,
							ImagePullPolicy: corev1.PullIfNotPresent,
							Args:            arguments,
							WorkingDir:      DataRoot,
							Env: []corev1.EnvVar{
								{Name: "TMPDIR", Value: TemporaryMountPath},
							},
							SecurityContext: &corev1.SecurityContext{
								AllowPrivilegeEscalation: &allowPrivilegeEscalation,
								ReadOnlyRootFilesystem:   &readOnlyRootFilesystem,
								RunAsNonRoot:             &runAsNonRoot,
								RunAsUser:                &runAsUser,
								RunAsGroup:               &runAsGroup,
								Capabilities: &corev1.Capabilities{
									Drop: append([]corev1.Capability(nil), dropAllCapabilities...),
								},
							},
							TerminationMessagePath:   "/dev/termination-log",
							TerminationMessagePolicy: corev1.TerminationMessageReadFile,
							VolumeMounts: []corev1.VolumeMount{
								{Name: DataVolumeName, MountPath: DataRoot},
								{Name: TemporaryVolumeName, MountPath: TemporaryMountPath},
							},
						},
					},
					Volumes: []corev1.Volume{
						{
							Name: DataVolumeName,
							VolumeSource: corev1.VolumeSource{
								PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{
									ClaimName: run.Spec.Execution.PVCName,
									ReadOnly:  false,
								},
							},
						},
						{
							Name: TemporaryVolumeName,
							VolumeSource: corev1.VolumeSource{
								EmptyDir: &corev1.EmptyDirVolumeSource{},
							},
						},
					},
				},
			},
		},
	}, nil
}

// ValidateJob rejects a live Job whose execution-affecting template differs
// from BuildJob. Only API-server metadata and the generated Job selector
// labels are normalized before comparison.
func ValidateJob(job *batchv1.Job, run *agentrunv1alpha1.AgentRun, action, specSHA string) error {
	if job == nil {
		return errors.New("Job is required")
	}
	expected, err := BuildJob(run, action, specSHA)
	if err != nil {
		return err
	}
	if job.Name != expected.Name || job.Namespace != expected.Namespace {
		return errors.New("Job identity does not match the deterministic template")
	}
	if !reflect.DeepEqual(job.Labels, expected.Labels) {
		return errors.New("Job labels do not exactly match the audited template")
	}
	if !reflect.DeepEqual(job.Annotations, expected.Annotations) {
		return errors.New("Job annotations do not exactly match the audited template")
	}
	if !reflect.DeepEqual(job.OwnerReferences, expected.OwnerReferences) {
		return errors.New("Job owner reference does not exactly match AgentRun")
	}

	actual := job.DeepCopy()
	actualSpec := actual.Spec.DeepCopy()
	expectedSpec := expected.Spec.DeepCopy()
	if err := validateGeneratedSelector(
		actualSpec.Selector,
		actualSpec.Template.Labels,
		job.Name,
		string(job.UID),
	); err != nil {
		return err
	}
	actualSpec.Selector = nil
	expectedSpec.Selector = nil
	actualSpec.Template.Labels = stripGeneratedJobLabels(actualSpec.Template.Labels)
	if !reflect.DeepEqual(actualSpec, expectedSpec) {
		return errors.New("Job execution template differs from the audited template")
	}
	return nil
}

func validateGeneratedSelector(
	selector *metav1.LabelSelector,
	labels map[string]string,
	jobName, jobUID string,
) error {
	if selector == nil {
		for _, key := range []string{
			"batch.kubernetes.io/controller-uid",
			"batch.kubernetes.io/job-name",
			"controller-uid",
			"job-name",
		} {
			if _, present := labels[key]; present {
				return errors.New("Job has generated labels without a selector")
			}
		}
		return nil
	}
	if jobUID == "" || len(selector.MatchExpressions) != 0 ||
		len(selector.MatchLabels) == 0 || len(selector.MatchLabels) > 2 {
		return errors.New("Job has an unexpected generated selector")
	}
	allowed := map[string]struct{}{"batch.kubernetes.io/controller-uid": {}, "controller-uid": {}}
	for key, value := range selector.MatchLabels {
		if _, ok := allowed[key]; !ok || value != jobUID || labels[key] != value {
			return errors.New("Job selector is not the API-generated controller UID selector")
		}
	}
	for key, want := range map[string]string{
		"batch.kubernetes.io/controller-uid": jobUID,
		"batch.kubernetes.io/job-name":       jobName,
		"controller-uid":                     jobUID,
		"job-name":                           jobName,
	} {
		if value, present := labels[key]; present && value != want {
			return fmt.Errorf("Job generated label %q does not match", key)
		}
	}
	return nil
}

func stripGeneratedJobLabels(labels map[string]string) map[string]string {
	copy := copyStringMap(labels)
	delete(copy, "batch.kubernetes.io/controller-uid")
	delete(copy, "batch.kubernetes.io/job-name")
	delete(copy, "controller-uid")
	delete(copy, "job-name")
	return copy
}

func deterministicJobName(runUID string, action JobAction, specSHA string) string {
	sum := sha256.Sum256([]byte(runUID + "\x00" + string(action) + "\x00" + specSHA))
	return "agentrun-" + string(action) + "-" + hex.EncodeToString(sum[:8])
}

func copyStringMap(source map[string]string) map[string]string {
	copy := make(map[string]string, len(source))
	for key, value := range source {
		copy[key] = value
	}
	return copy
}
