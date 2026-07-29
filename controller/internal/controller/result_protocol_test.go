package controller

import (
	"encoding/json"
	"strings"
	"testing"

	agentrunv1alpha1 "github.com/tiramitree/benchhandoff/controller/api/v1alpha1"
)

const testAgentRunUID = "12345678-1234-4abc-8def-1234567890ab"

func terminationMessage(action, outcome string) string {
	fields := map[string]string{
		"protocol":               TerminationProtocol,
		"action":                 action,
		"outcome":                outcome,
		"agent_run_uid":          testAgentRunUID,
		"execution_spec_sha256":  strings.Repeat("a", 64),
		"run_id":                 "",
		"resume_decision_sha256": "",
		"bundle_sha256":          "",
		"error_code":             "",
	}
	switch StepOutcome(outcome) {
	case OutcomeCompleted:
		fields["run_id"] = strings.Repeat("b", 32)
		fields["bundle_sha256"] = strings.Repeat("c", 64)
		if JobAction(action) == ActionResume {
			fields["resume_decision_sha256"] = strings.Repeat("d", 64)
		}
	case OutcomeAwaitingApproval:
		fields["run_id"] = strings.Repeat("b", 32)
		fields["resume_decision_sha256"] = strings.Repeat("d", 64)
	case OutcomeVerified:
		fields["run_id"] = strings.Repeat("b", 32)
		fields["bundle_sha256"] = strings.Repeat("c", 64)
	case OutcomeBlocked:
		fields["error_code"] = string(ErrorExecutionFailed)
	}
	payload, err := json.Marshal(fields)
	if err != nil {
		panic(err)
	}
	return string(payload)
}

func TestParseTerminationMessageAcceptedOutcomes(t *testing.T) {
	tests := []struct {
		action  JobAction
		outcome StepOutcome
		phase   agentrunv1alpha1.AgentRunPhase
	}{
		{ActionStart, OutcomeCompleted, agentrunv1alpha1.PhaseVerifying},
		{ActionStart, OutcomeAwaitingApproval, agentrunv1alpha1.PhaseAwaitingApproval},
		{ActionResume, OutcomeCompleted, agentrunv1alpha1.PhaseVerifying},
		{ActionVerify, OutcomeVerified, agentrunv1alpha1.PhaseSucceeded},
		{ActionStart, OutcomeBlocked, agentrunv1alpha1.PhaseBlocked},
		{ActionResume, OutcomeBlocked, agentrunv1alpha1.PhaseBlocked},
		{ActionVerify, OutcomeBlocked, agentrunv1alpha1.PhaseBlocked},
	}
	for _, test := range tests {
		t.Run(string(test.action)+"/"+string(test.outcome), func(t *testing.T) {
			result, err := ParseTerminationMessage(
				terminationMessage(string(test.action), string(test.outcome)),
			)
			if err != nil {
				t.Fatalf("ParseTerminationMessage: %v", err)
			}
			if result.Phase() != test.phase {
				t.Fatalf("phase = %q, want %q", result.Phase(), test.phase)
			}
			if err := ValidateStepResult(
				result,
				testAgentRunUID,
				string(test.action),
				strings.Repeat("a", 64),
			); err != nil {
				t.Fatalf("ValidateStepResult: %v", err)
			}
		})
	}
}

func TestParseTerminationMessageRejectsSchemaDrift(t *testing.T) {
	valid := terminationMessage("start", "completed")
	var fields map[string]string
	if err := json.Unmarshal([]byte(valid), &fields); err != nil {
		t.Fatal(err)
	}
	delete(fields, "bundle_sha256")
	missing, _ := json.Marshal(fields)
	if _, err := ParseTerminationMessage(string(missing)); err == nil {
		t.Fatal("missing key unexpectedly accepted")
	}

	fields["bundle_sha256"] = strings.Repeat("c", 64)
	fields["detail"] = "must never be exposed"
	unknown, _ := json.Marshal(fields)
	if _, err := ParseTerminationMessage(string(unknown)); err == nil {
		t.Fatal("unknown key unexpectedly accepted")
	}

	duplicate := strings.Replace(valid, `"protocol":`, `"protocol":"duplicate","protocol":`, 1)
	if _, err := ParseTerminationMessage(duplicate); err == nil {
		t.Fatal("duplicate key unexpectedly accepted")
	}
	nonString := strings.Replace(valid, `"error_code":""`, `"error_code":7`, 1)
	if _, err := ParseTerminationMessage(nonString); err == nil {
		t.Fatal("non-string value unexpectedly accepted")
	}
	if _, err := ParseTerminationMessage(valid + `{}`); err == nil {
		t.Fatal("trailing object unexpectedly accepted")
	}
	if _, err := ParseTerminationMessage(strings.Repeat(" ", MaxTerminationBytes+1)); err == nil {
		t.Fatal("oversized message unexpectedly accepted")
	}
}

func TestParseTerminationMessageRejectsInvalidOutcomeCombinations(t *testing.T) {
	if _, err := ParseTerminationMessage(
		terminationMessage("resume", "awaiting_approval"),
	); err == nil {
		t.Fatal("resume awaiting_approval unexpectedly accepted")
	}
	if _, err := ParseTerminationMessage(
		terminationMessage("verify", "completed"),
	); err == nil {
		t.Fatal("verify completed unexpectedly accepted")
	}

	var fields map[string]string
	if err := json.Unmarshal(
		[]byte(terminationMessage("start", "blocked")),
		&fields,
	); err != nil {
		t.Fatal(err)
	}
	fields["run_id"] = strings.Repeat("b", 32)
	blockedWithEvidence, _ := json.Marshal(fields)
	if _, err := ParseTerminationMessage(string(blockedWithEvidence)); err == nil {
		t.Fatal("blocked result with run evidence unexpectedly accepted")
	}
	fields["run_id"] = ""
	fields["error_code"] = "secret_path_error"
	badCode, _ := json.Marshal(fields)
	if _, err := ParseTerminationMessage(string(badCode)); err == nil {
		t.Fatal("unregistered error code unexpectedly accepted")
	}
}

func TestValidateStepResultRejectsBindingMismatch(t *testing.T) {
	result, err := ParseTerminationMessage(terminationMessage("start", "completed"))
	if err != nil {
		t.Fatal(err)
	}
	tests := []struct {
		uid, action, hash string
	}{
		{"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "start", strings.Repeat("a", 64)},
		{testAgentRunUID, "verify", strings.Repeat("a", 64)},
		{testAgentRunUID, "start", strings.Repeat("f", 64)},
	}
	for _, test := range tests {
		if err := ValidateStepResult(result, test.uid, test.action, test.hash); err == nil {
			t.Fatalf("binding mismatch unexpectedly accepted: %#v", test)
		}
	}
}
