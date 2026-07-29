package controller

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"path"
	"regexp"
	"strings"
	"unicode/utf8"

	agentrunv1alpha1 "github.com/tiramitree/benchhandoff/controller/api/v1alpha1"
	"k8s.io/apimachinery/pkg/util/validation"
)

const (
	DataRoot          = "/benchhandoff-data"
	SuiteRoot         = DataRoot + "/suites"
	RunRoot           = DataRoot + "/runs"
	maxSuitePathBytes = 512
	minDeadline       = int64(30)
	maxDeadline       = int64(30 * 60)
)

var (
	sha256Pattern     = regexp.MustCompile(`^[0-9a-f]{64}$`)
	suitePathPattern  = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._/-]*$`)
	imageNamePattern  = regexp.MustCompile(`^[a-z0-9][a-z0-9._:/-]*[a-z0-9]$|^[a-z0-9]$`)
	imageDigestSuffix = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)
)

// ValidateExecutionSpec checks every value that can influence a runner Job.
// It deliberately rejects ambiguous, host-dependent, or unbounded inputs.
func ValidateExecutionSpec(spec agentrunv1alpha1.ExecutionSpec) error {
	if spec.PVCName == "" {
		return errors.New("pvcName is required")
	}
	if problems := validation.IsDNS1123Subdomain(spec.PVCName); len(problems) != 0 {
		return fmt.Errorf("pvcName is not a valid DNS-1123 subdomain: %s", strings.Join(problems, "; "))
	}
	if _, err := ResolveSuitePath(spec.SuitePath); err != nil {
		return err
	}
	if !IsSHA256(spec.SuiteSHA256) {
		return errors.New("suiteSHA256 must be 64 lowercase hexadecimal characters")
	}
	if err := validatePinnedImage(spec.RunnerImage); err != nil {
		return err
	}
	if spec.ActiveDeadlineSeconds < minDeadline || spec.ActiveDeadlineSeconds > maxDeadline {
		return fmt.Errorf("activeDeadlineSeconds must be between %d and %d", minDeadline, maxDeadline)
	}
	return nil
}

// ValidateAgentRunSpec validates execution inputs and an optional explicit
// resume-decision approval.
func ValidateAgentRunSpec(spec agentrunv1alpha1.AgentRunSpec) error {
	if err := ValidateExecutionSpec(spec.Execution); err != nil {
		return err
	}
	if spec.ResumeDecisionSHA256 != "" && !IsSHA256(spec.ResumeDecisionSHA256) {
		return errors.New("resumeDecisionSHA256 must be empty or 64 lowercase hexadecimal characters")
	}
	return nil
}

// ResolveSuitePath returns the fixed in-container absolute suite path after
// proving that suitePath is a normalized relative POSIX path below SuiteRoot.
func ResolveSuitePath(suitePath string) (string, error) {
	if suitePath == "" {
		return "", errors.New("suitePath is required")
	}
	if len(suitePath) > maxSuitePathBytes {
		return "", fmt.Errorf("suitePath exceeds %d bytes", maxSuitePathBytes)
	}
	if !utf8.ValidString(suitePath) || strings.ContainsRune(suitePath, '\x00') {
		return "", errors.New("suitePath must be valid UTF-8 without NUL")
	}
	if strings.TrimSpace(suitePath) != suitePath {
		return "", errors.New("suitePath must not have surrounding whitespace")
	}
	if strings.ContainsRune(suitePath, '\\') {
		return "", errors.New("suitePath must use POSIX separators")
	}
	if path.IsAbs(suitePath) || strings.HasPrefix(suitePath, "/") {
		return "", errors.New("suitePath must be relative")
	}
	if path.Clean(suitePath) != suitePath || suitePath == "." || strings.HasSuffix(suitePath, "/") {
		return "", errors.New("suitePath must be a normalized POSIX file path")
	}
	if !suitePathPattern.MatchString(suitePath) {
		return "", errors.New("suitePath contains unsupported characters")
	}
	for _, component := range strings.Split(suitePath, "/") {
		if component == "" || component == "." || component == ".." {
			return "", errors.New("suitePath contains an unsafe component")
		}
	}
	if !strings.HasSuffix(suitePath, ".toml") {
		return "", errors.New("suitePath must identify a .toml suite")
	}

	resolved := path.Join(SuiteRoot, suitePath)
	if !strings.HasPrefix(resolved, SuiteRoot+"/") {
		return "", errors.New("suitePath escapes the suite root")
	}
	return resolved, nil
}

// CanonicalExecutionSpecSHA returns the SHA-256 of the validated, versioned
// canonical execution-spec representation. Resume approval is intentionally
// excluded because it does not change the execution environment.
func CanonicalExecutionSpecSHA(spec agentrunv1alpha1.ExecutionSpec) (string, error) {
	if err := ValidateExecutionSpec(spec); err != nil {
		return "", err
	}
	canonical := struct {
		SchemaVersion         string `json:"schemaVersion"`
		PVCName               string `json:"pvcName"`
		SuitePath             string `json:"suitePath"`
		SuiteSHA256           string `json:"suiteSHA256"`
		RunnerImage           string `json:"runnerImage"`
		ActiveDeadlineSeconds int64  `json:"activeDeadlineSeconds"`
	}{
		SchemaVersion:         "control.benchhandoff.dev/execution-spec/v1alpha1",
		PVCName:               spec.PVCName,
		SuitePath:             spec.SuitePath,
		SuiteSHA256:           spec.SuiteSHA256,
		RunnerImage:           spec.RunnerImage,
		ActiveDeadlineSeconds: spec.ActiveDeadlineSeconds,
	}
	payload, err := json.Marshal(canonical)
	if err != nil {
		return "", fmt.Errorf("marshal canonical execution spec: %w", err)
	}
	sum := sha256.Sum256(payload)
	return hex.EncodeToString(sum[:]), nil
}

// IsSHA256 reports whether value is a canonical lowercase SHA-256 string.
func IsSHA256(value string) bool {
	return sha256Pattern.MatchString(value)
}

func validatePinnedImage(image string) error {
	if len(image) > 512 {
		return errors.New("runnerImage exceeds 512 bytes")
	}
	if strings.TrimSpace(image) != image || strings.ContainsAny(image, " \t\r\n") {
		return errors.New("runnerImage must not contain whitespace")
	}
	if strings.Contains(image, "://") {
		return errors.New("runnerImage must be an OCI image reference, not a URL")
	}
	parts := strings.Split(image, "@")
	if len(parts) != 2 || parts[0] == "" || !imageDigestSuffix.MatchString(parts[1]) {
		return errors.New("runnerImage must be name@sha256:<64 lowercase hex>")
	}
	name := parts[0]
	if len(name) > 255 || !imageNamePattern.MatchString(name) {
		return errors.New("runnerImage name is invalid")
	}
	if strings.Contains(name, "//") || strings.Contains(name, "..") ||
		strings.HasPrefix(name, ".") || strings.HasPrefix(name, "-") ||
		strings.HasSuffix(name, ".") || strings.HasSuffix(name, "-") {
		return errors.New("runnerImage name is not normalized")
	}
	return nil
}
