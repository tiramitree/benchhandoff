package controller

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"regexp"

	agentrunv1alpha1 "github.com/tiramitree/benchhandoff/controller/api/v1alpha1"
)

const (
	TerminationProtocol = "benchhandoff-controller-step/v1"
	MaxTerminationBytes = 1024
)

type JobAction string

const (
	ActionStart  JobAction = "start"
	ActionResume JobAction = "resume"
	ActionVerify JobAction = "verify"
)

type StepOutcome string

const (
	OutcomeCompleted        StepOutcome = "completed"
	OutcomeAwaitingApproval StepOutcome = "awaiting_approval"
	OutcomeVerified         StepOutcome = "verified"
	OutcomeBlocked          StepOutcome = "blocked"
)

type StepErrorCode string

const (
	ErrorInvalidRequest  StepErrorCode = "invalid_request"
	ErrorExecutionFailed StepErrorCode = "execution_failed"
	ErrorEvidenceInvalid StepErrorCode = "evidence_invalid"
	ErrorInternal        StepErrorCode = "internal_error"
)

var (
	agentRunUIDPattern = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)
	runIDPattern       = regexp.MustCompile(`^[0-9a-f]{32}$`)
)

// StepResult is the exact nine-string-key termination-message protocol emitted
// by python -m benchhandoff.controller_step.
type StepResult struct {
	Protocol             string
	Action               JobAction
	Outcome              StepOutcome
	AgentRunUID          string
	ExecutionSpecSHA256  string
	RunID                string
	ResumeDecisionSHA256 string
	BundleSHA256         string
	ErrorCode            StepErrorCode
}

// ParseTerminationMessage parses a bounded JSON object with exactly the nine
// protocol keys and validates all intrinsic outcome invariants.
func ParseTerminationMessage(raw string) (StepResult, error) {
	fields, err := decodeExactTerminationObject([]byte(raw))
	if err != nil {
		return StepResult{}, err
	}
	result := StepResult{
		Protocol:             fields["protocol"],
		Action:               JobAction(fields["action"]),
		Outcome:              StepOutcome(fields["outcome"]),
		AgentRunUID:          fields["agent_run_uid"],
		ExecutionSpecSHA256:  fields["execution_spec_sha256"],
		RunID:                fields["run_id"],
		ResumeDecisionSHA256: fields["resume_decision_sha256"],
		BundleSHA256:         fields["bundle_sha256"],
		ErrorCode:            StepErrorCode(fields["error_code"]),
	}
	if result.Protocol != TerminationProtocol {
		return StepResult{}, fmt.Errorf("unsupported termination protocol %q", result.Protocol)
	}
	if err := validateStepResultInvariants(result); err != nil {
		return StepResult{}, err
	}
	return result, nil
}

// ValidateStepResult proves that an already parsed result belongs to the exact
// Job owner, action, and canonical execution spec.
func ValidateStepResult(result StepResult, runUID, action, specSHA string) error {
	if err := validateStepResultInvariants(result); err != nil {
		return err
	}
	if !agentRunUIDPattern.MatchString(runUID) {
		return errors.New("expected AgentRun UID must be a lowercase UUID")
	}
	if !validAction(JobAction(action)) {
		return fmt.Errorf("unsupported expected action %q", action)
	}
	if !IsSHA256(specSHA) {
		return errors.New("expected execution spec hash must be 64 lowercase hexadecimal characters")
	}
	if result.AgentRunUID != runUID {
		return errors.New("termination agent_run_uid does not match Job owner")
	}
	if result.Action != JobAction(action) {
		return fmt.Errorf("termination action %q does not match expected %q", result.Action, action)
	}
	if result.ExecutionSpecSHA256 != specSHA {
		return errors.New("termination execution_spec_sha256 does not match Job annotation")
	}
	return nil
}

// Phase returns the controller phase proved by this result. A completed runner
// still requires a separate verify action before it can be Succeeded.
func (result StepResult) Phase() agentrunv1alpha1.AgentRunPhase {
	switch result.Outcome {
	case OutcomeCompleted:
		return agentrunv1alpha1.PhaseVerifying
	case OutcomeAwaitingApproval:
		return agentrunv1alpha1.PhaseAwaitingApproval
	case OutcomeVerified:
		return agentrunv1alpha1.PhaseSucceeded
	default:
		return agentrunv1alpha1.PhaseBlocked
	}
}

func validateStepResultInvariants(result StepResult) error {
	if result.Protocol != TerminationProtocol {
		return fmt.Errorf("unsupported termination protocol %q", result.Protocol)
	}
	if !validAction(result.Action) {
		return fmt.Errorf("unsupported termination action %q", result.Action)
	}
	if !agentRunUIDPattern.MatchString(result.AgentRunUID) {
		return errors.New("termination agent_run_uid is not a lowercase UUID")
	}
	if !IsSHA256(result.ExecutionSpecSHA256) {
		return errors.New("termination execution_spec_sha256 is invalid")
	}

	switch result.Outcome {
	case OutcomeCompleted:
		if result.Action != ActionStart && result.Action != ActionResume {
			return errors.New("completed outcome is valid only for start or resume")
		}
		if !runIDPattern.MatchString(result.RunID) || !IsSHA256(result.BundleSHA256) {
			return errors.New("completed outcome requires a valid run_id and bundle_sha256")
		}
		if result.ErrorCode != "" {
			return errors.New("completed outcome must not carry error_code")
		}
		if result.Action == ActionStart && result.ResumeDecisionSHA256 != "" {
			return errors.New("start completion must not carry resume_decision_sha256")
		}
		if result.Action == ActionResume && !IsSHA256(result.ResumeDecisionSHA256) {
			return errors.New("resume completion must echo a resume_decision_sha256")
		}
	case OutcomeAwaitingApproval:
		if result.Action != ActionStart {
			return errors.New("awaiting_approval outcome is valid only for start")
		}
		if !runIDPattern.MatchString(result.RunID) || !IsSHA256(result.ResumeDecisionSHA256) {
			return errors.New("awaiting_approval outcome requires a valid run_id and resume_decision_sha256")
		}
		if result.BundleSHA256 != "" || result.ErrorCode != "" {
			return errors.New("awaiting_approval outcome must not carry bundle_sha256 or error_code")
		}
	case OutcomeVerified:
		if result.Action != ActionVerify {
			return errors.New("verified outcome is valid only for verify")
		}
		if !runIDPattern.MatchString(result.RunID) || !IsSHA256(result.BundleSHA256) {
			return errors.New("verified outcome requires a valid run_id and bundle_sha256")
		}
		if result.ResumeDecisionSHA256 != "" || result.ErrorCode != "" {
			return errors.New("verified outcome must not carry resume_decision_sha256 or error_code")
		}
	case OutcomeBlocked:
		if result.RunID != "" || result.ResumeDecisionSHA256 != "" || result.BundleSHA256 != "" {
			return errors.New("blocked outcome must not carry run, decision, or bundle evidence")
		}
		if !validErrorCode(result.ErrorCode) {
			return fmt.Errorf("blocked outcome has unsupported error_code %q", result.ErrorCode)
		}
	default:
		return fmt.Errorf("unsupported termination outcome %q", result.Outcome)
	}
	return nil
}

func decodeExactTerminationObject(message []byte) (map[string]string, error) {
	if len(message) == 0 {
		return nil, errors.New("termination message is empty")
	}
	if len(message) > MaxTerminationBytes {
		return nil, fmt.Errorf("termination message exceeds %d bytes", MaxTerminationBytes)
	}
	decoder := json.NewDecoder(bytes.NewReader(message))
	open, err := decoder.Token()
	if err != nil {
		return nil, fmt.Errorf("decode termination message: %w", err)
	}
	if delimiter, ok := open.(json.Delim); !ok || delimiter != '{' {
		return nil, errors.New("termination message must be one JSON object")
	}
	allowed := map[string]struct{}{
		"protocol": {}, "action": {}, "outcome": {}, "agent_run_uid": {},
		"execution_spec_sha256": {}, "run_id": {}, "resume_decision_sha256": {},
		"bundle_sha256": {}, "error_code": {},
	}
	fields := make(map[string]string, len(allowed))
	for decoder.More() {
		token, err := decoder.Token()
		if err != nil {
			return nil, fmt.Errorf("decode termination key: %w", err)
		}
		key, ok := token.(string)
		if !ok {
			return nil, errors.New("termination object key is not a string")
		}
		if _, ok := allowed[key]; !ok {
			return nil, fmt.Errorf("termination message has unknown key %q", key)
		}
		if _, duplicate := fields[key]; duplicate {
			return nil, fmt.Errorf("termination message has duplicate key %q", key)
		}
		var value string
		if err := decoder.Decode(&value); err != nil {
			return nil, fmt.Errorf("termination key %q must contain a string: %w", key, err)
		}
		fields[key] = value
	}
	closeToken, err := decoder.Token()
	if err != nil {
		return nil, fmt.Errorf("decode termination object end: %w", err)
	}
	if delimiter, ok := closeToken.(json.Delim); !ok || delimiter != '}' {
		return nil, errors.New("termination message has an invalid object end")
	}
	if token, err := decoder.Token(); err != io.EOF {
		if err != nil {
			return nil, fmt.Errorf("decode trailing termination data: %w", err)
		}
		return nil, fmt.Errorf("termination message has trailing token %v", token)
	}
	if len(fields) != len(allowed) {
		for key := range allowed {
			if _, ok := fields[key]; !ok {
				return nil, fmt.Errorf("termination message is missing key %q", key)
			}
		}
	}
	return fields, nil
}

func validAction(action JobAction) bool {
	return action == ActionStart || action == ActionResume || action == ActionVerify
}

func validErrorCode(code StepErrorCode) bool {
	return code == ErrorInvalidRequest || code == ErrorExecutionFailed ||
		code == ErrorEvidenceInvalid || code == ErrorInternal
}
