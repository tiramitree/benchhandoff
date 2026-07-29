# AI Assistance Disclosure

This implementation, including the v0.2 execution-context and process-scope
work, the v0.3 closed-world workspace boundary, and the v0.4 optional
`AgentRun` controller, plus the v0.5 two-manager Lease/takeover work, tests, and
documentation, was drafted with OpenAI Codex assistance
under the user's direction.

The assistant:

- translated the stated reliability requirements into a new implementation;
- wrote Python and Go source code, Kubernetes manifests, tests, examples, and
  documentation;
- ran repository unit, race, synthetic benchmark, and disposable kind
  integration checks locally or in public CI as explicitly reported; and
- reported test boundaries and known limitations.

Checked-in tests and workflows define maintainer-operated validation
procedures. Their results apply only to the exact source revision on which
they run and are not independent validation, production deployment, or
adoption.

No production deployment, independent user, upstream acceptance, performance
advantage, or external adoption is asserted by this disclosure. A passing local
test demonstrates only the behavior covered by that test.

Future public claims should be tied to immutable test output, a released commit,
or independently inspectable external evidence. Human review remains necessary
for licensing, security-sensitive deployment, and any outward representation
of authorship or organizational affiliation.
