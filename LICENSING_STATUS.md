# License Pending

No open-source license has been selected for this local worktree.

Until the user makes an explicit licensing decision, do not assume permission
to copy, redistribute, publish, sublicense, or incorporate this work into
another project. Ordinary copyright defaults apply.

Before any public release:

1. confirm ownership and contribution boundaries;
2. check the working project and package names;
3. select an appropriate license through an explicit user decision;
4. add the final license text and package metadata; and
5. re-audit fixtures, disclosures, and third-party materials.

This file is a status notice, not a software license.

## Automation boundary

The repository validator currently accepts this pending state. The package
gate requires a final state and therefore remains intentionally closed.

Only the exact owner responses `Apache-2.0` or `MIT` authorize the corresponding
transition. After that decision, `tools/finalize_license.py` can prepare and
apply the commit-bound transition; it accepts only canonical candidate bytes,
updates the PEP 639 metadata, removes this notice, and verifies the result. The
tool does not authorize or perform a commit, public upload, tag, or release.
