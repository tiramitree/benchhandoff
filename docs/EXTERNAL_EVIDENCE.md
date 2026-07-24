# External Evidence Ledger

BenchHandoff keeps external evidence in
[`EXTERNAL_EVIDENCE.json`](../EXTERNAL_EVIDENCE.json). The ledger is deliberately
separate from local validation, CI, repository traffic, and maintainer-authored
examples.

As of 2026-07-24, the public baseline is:

| Evidence class | Verified count |
|---|---:|
| Independent reproduction reports | 0 |
| Independent users | 0 |
| Institutional adopters | 0 |
| Third-party reviews | 0 |

Zero is an intentional factual value, not a missing-data placeholder.

## What can count

- **Independent reproduction report**: one outside person publishes a bounded
  attempt against an exact source commit with inspectable commands, environment,
  result counts, deviations, and evidence.
- **Independent user**: one outside person publicly states a concrete,
  non-maintainer use of an exact source commit. The count is deduplicated by
  public subject identity.
- **Institutional adopter**: an authorized public representative links an
  organization to a concrete use of an exact source commit. The count is
  deduplicated by public organization identity.
- **Third-party review**: an outside person publishes a substantive technical
  review tied to an exact source commit and inspectable evidence.

Every counted record requires a public HTTPS evidence URL without query
parameters, a full lowercase source commit, an independent relationship,
consent to list the public identity and evidence, and human verification. A
record is not anonymous. URL fragments may point to a relevant section but are
ignored when detecting duplicate evidence pages.

## What does not count

The following never establish a ledger count:

- a maintainer run, self-authored example, local benchmark, or CI job;
- an opened Issue or Pull Request that has not been human-reviewed and merged
  into the ledger;
- a star, fork, clone, view, download, package-install counter, social
  impression, or recruiter message;
- a bot, generated testimonial, template copy, or synthetic account;
- a collaborator, employee, contractor, client/vendor, investor, funded
  participant, course/lab peer, family member, or other affiliated party; or
- private, unverifiable, or non-consensual usage.

These exclusions prevent attention, automation, and relationships from being
rewritten as independent adoption.

## Submission and review flow

1. Use the independent-reproduction form for one bounded reproduction attempt.
   Use the external-evidence form for independent use, institutional adoption,
   or third-party review.
2. Keep the Issue public and minimal. Do not submit raw runs, logs, private
   inputs, production details, absolute paths, credentials, tokens, or personal
   data beyond the identity you explicitly consent to list.
3. A maintainer manually checks the exact source, scope, public evidence,
   relationship disclosure, consent, and duplicate identity/evidence boundary.
4. If the evidence meets the definition, add one canonical record, recompute
   the derived counts, and run:

   ```bash
   python tools/verify_external_evidence.py
   ```

5. Only a reviewed ledger change merged into the repository changes the public
   count. Closing or labeling an Issue does not.

Records are sorted by `id`. Repeated independent-user and institutional records
for the same Unicode-normalized, case-insensitive public subject count once.
Reproduction reports and third-party reviews count verified records, and
evidence pages must be unique.

## Retraction and correction

Do not delete a once-counted record to hide history. Change its status to
`retracted`, add the retraction date and a short reason, update the counts, and
retain the original public evidence fields. Retracted records do not count.

If public evidence must be removed for safety, privacy, or consent reasons,
retain only the smallest non-sensitive correction record the affected person
has approved. Safety and privacy take priority over historical detail.

## Validator boundary

`tools/verify_external_evidence.py` checks a bounded regular non-linked file,
strict canonical JSON, exact fields, dates, IDs, full source commits, public-form
HTTPS URLs, relationship and consent fields, record ordering, duplicate IDs and
URLs, retraction rules, subject deduplication, and derived counts.

It performs no network request. It cannot prove that an identity is genuine,
that a relationship is independent, that a URL remains live, that a statement
is true, or that use was successful. Those are human-review responsibilities;
the validator prevents structural drift and arithmetic inflation only.
