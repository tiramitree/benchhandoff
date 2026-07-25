# Support

BenchHandoff has one GitHub-only early release and no package-registry release,
support SLA, guaranteed response time, compatibility promise, or
production-support commitment.

Use the repository's structured issue forms for reproducible bugs, independent
reproduction reports, and proposed external-use or review evidence. Use the
security process in [SECURITY.md](SECURITY.md) for vulnerabilities. General
questions should be kept to a minimal, public, non-sensitive example.

Before filing an issue:

1. identify the exact release or commit;
2. record the operating system, Python implementation, and Python version;
3. reduce the problem to a synthetic suite with no private inputs;
4. run `benchhandoff verify RUN_DIR` when that is safe and relevant; and
5. describe the observed exit code and failure category without pasting
   sensitive output.

Never upload a raw run directory, command log, absolute path, local username,
token, credential, private dataset, environment dump, or confidential command
line. Reduce a problem to new synthetic inputs and describe only the bounded
failure category and exit code; do not attach or edit a private run tree as a
substitute for that reduction.

Support questions do not establish external adoption, production use, or a
compatibility guarantee. A response may explain the documented boundary rather
than add a new feature or recovery path.

Opening an external-evidence Issue also does not create a count. Only a
human-reviewed record merged into `EXTERNAL_EVIDENCE.json` counts under
[the public ledger rules](docs/EXTERNAL_EVIDENCE.md).
