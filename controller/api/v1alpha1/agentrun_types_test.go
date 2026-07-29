package v1alpha1

import (
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

func TestAgentRunDeepCopyDoesNotAliasStatus(t *testing.T) {
	run := &AgentRun{
		Status: AgentRunStatus{
			ActiveJobRef: &JobRef{Name: "job-a"},
			Conditions: []metav1.Condition{
				{Type: "Ready", Message: "original"},
			},
		},
	}
	copy := run.DeepCopy()
	copy.Status.ActiveJobRef.Name = "job-b"
	copy.Status.Conditions[0].Message = "changed"
	if run.Status.ActiveJobRef.Name != "job-a" {
		t.Fatal("DeepCopy aliased ActiveJobRef")
	}
	if run.Status.Conditions[0].Message != "original" {
		t.Fatal("DeepCopy aliased Conditions")
	}
}
