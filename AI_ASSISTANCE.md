# AI Assistance Disclosure

This implementation, including the v0.2 execution-context and process-scope
work, the v0.3 closed-world workspace boundary, and the v0.4 optional
`AgentRun` controller, plus the unreleased v0.5 two-manager Lease/takeover
candidate, tests, and documentation, was drafted with OpenAI Codex assistance
under the user's direction.

The assistant:

- translated the stated reliability requirements into a new implementation;
- wrote Python and Go source code, Kubernetes manifests, tests, examples, and
  documentation;
- ran repository unit, race, synthetic benchmark, and disposable kind
  integration checks locally or in public CI as explicitly reported; and
- reported test boundaries and known limitations.

For the current v0.5 candidate, the reported evidence is local only:
public-privacy verification passed; the Python suite reported 229 passes and 4
Windows capability skips; and Go formatting, module-tidiness verification,
module verification, `go vet`, and unit tests passed. A local trimpath manager
build passed with `-buildvcs=false`, required by this nested worktree's separate
repository boundary. The local Windows environment cannot run the Go race
test. No immutable v0.5 candidate commit has yet passed both the ordinary
public CI matrix and the real-kind gate, and no v0.5
release or takeover artifact is claimed.

No production deployment, independent user, upstream acceptance, performance
advantage, or external adoption is asserted by this disclosure. A passing local
test demonstrates only the behavior covered by that test.

Future public claims should be tied to immutable test output, a released commit,
or independently inspectable external evidence. Human review remains necessary
for licensing, security-sensitive deployment, and any outward representation
of authorship or organizational affiliation.
