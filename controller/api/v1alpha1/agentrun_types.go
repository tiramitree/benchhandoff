package v1alpha1

import metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

// AgentRunPhase is a controller-observed phase. A phase is evidence about
// controller progress, not a claim that a workload ran in production.
type AgentRunPhase string

const (
	PhasePending          AgentRunPhase = "Pending"
	PhaseRunning          AgentRunPhase = "Running"
	PhaseAwaitingApproval AgentRunPhase = "AwaitingApproval"
	PhaseVerifying        AgentRunPhase = "Verifying"
	PhaseSucceeded        AgentRunPhase = "Succeeded"
	PhaseBlocked          AgentRunPhase = "Blocked"
)

// ExecutionSpec identifies the immutable execution inputs bound into a Job.
type ExecutionSpec struct {
	PVCName               string `json:"pvcName"`
	SuitePath             string `json:"suitePath"`
	SuiteSHA256           string `json:"suiteSHA256"`
	RunnerImage           string `json:"runnerImage"`
	ActiveDeadlineSeconds int64  `json:"activeDeadlineSeconds"`
}

// AgentRunSpec is the requested execution plus an optional, exact resume
// decision approval. The approval is data, not an instruction to weaken any
// runtime validation.
type AgentRunSpec struct {
	Execution            ExecutionSpec `json:"execution"`
	ResumeDecisionSHA256 string        `json:"resumeDecisionSHA256,omitempty"`
}

// JobRef binds an observed Job to the action that it was created to perform.
type JobRef struct {
	Name   string `json:"name"`
	UID    string `json:"uid"`
	Action string `json:"action"`
}

// AgentRunStatus contains controller-observed, reproducible evidence pointers.
type AgentRunStatus struct {
	ObservedGeneration   int64              `json:"observedGeneration,omitempty"`
	ExecutionSpecSHA256  string             `json:"executionSpecSHA256,omitempty"`
	Phase                string             `json:"phase,omitempty"`
	RunID                string             `json:"runID,omitempty"`
	ResumeDecisionSHA256 string             `json:"resumeDecisionSHA256,omitempty"`
	BundleSHA256         string             `json:"bundleSHA256,omitempty"`
	ActiveJobRef         *JobRef            `json:"activeJobRef,omitempty"`
	Conditions           []metav1.Condition `json:"conditions,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status

// AgentRun requests one auditable BenchHandoff execution.
type AgentRun struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   AgentRunSpec   `json:"spec"`
	Status AgentRunStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// AgentRunList is a list of AgentRun resources.
type AgentRunList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []AgentRun `json:"items"`
}
