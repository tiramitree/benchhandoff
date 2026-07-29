from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

import benchhandoff.controller_step as controller_step
from benchhandoff.controller_step import MAX_TERMINATION_BYTES, PROTOCOL
from benchhandoff.storage import file_identity
from benchhandoff.workspace import WORKSPACE_POLICY, snapshot_workspace
from tests.test_benchhandoff import (
    FAIL_ONCE_WORKER,
    SUCCESS_WORKER,
    write_suite,
)
from tests.workspace_temp import WorkspaceTemporaryDirectory

AGENT_RUN_UID = "12345678-1234-4234-8234-123456789abc"
ALWAYS_FAIL_WORKER = """\
raise SystemExit(75)
"""
EXPECTED_KEYS = {
    "protocol",
    "action",
    "outcome",
    "agent_run_uid",
    "execution_spec_sha256",
    "run_id",
    "resume_decision_sha256",
    "bundle_sha256",
    "error_code",
}

_CONTEXT_BYTES = b'{"kind":"controller-step-test","schema_version":1}\n'


def _write_controller_suite(root: Path, worker: str) -> Path:
    workspace = root / "workspace"
    (workspace / "results").mkdir(parents=True)
    (workspace / "context.json").write_bytes(_CONTEXT_BYTES)
    (workspace / "input.txt").write_text("payload\n", encoding="utf-8")
    (workspace / "worker.py").write_text(worker, encoding="utf-8")

    manifest = root / "workspace.snapshot.json"
    snapshot_workspace(workspace, manifest)
    manifest_identity = file_identity(manifest, label="controller test manifest")
    context_sha256 = hashlib.sha256(_CONTEXT_BYTES).hexdigest()
    argv = [
        Path(sys.executable).name,
        "worker.py",
        "input.txt",
        "results/result.txt",
    ]
    suite = root / "suite.toml"
    suite.write_text(
        "\n".join(
            [
                "version = 3",
                'name = "controller-step-test"',
                "",
                "[context]",
                'path = "context.json"',
                'media_type = "application/vnd.benchhandoff.controller-test+json"',
                f'digest = "sha256:{context_sha256}"',
                f"size = {len(_CONTEXT_BYTES)}",
                "",
                "[workspace]",
                'root = "workspace"',
                'manifest = "workspace.snapshot.json"',
                f'digest = "sha256:{manifest_identity["sha256"]}"',
                f'size = {manifest_identity["size"]}',
                f"policy = {json.dumps(WORKSPACE_POLICY)}",
                "",
                "[[task]]",
                'id = "one"',
                "argv = [" + ", ".join(json.dumps(value) for value in argv) + "]",
                'inputs = ["context.json", "input.txt", "worker.py"]',
                'outputs = ["results/result.txt"]',
                "",
            ]
        ),
        encoding="utf-8",
    )
    return suite


class ControllerStepTests(unittest.TestCase):
    def _layout(self, temporary: str, worker: str) -> tuple[Path, Path, str]:
        data_root = Path(temporary) / "data"
        (data_root / "suites").mkdir(parents=True)
        (data_root / "runs").mkdir()
        suite = _write_controller_suite(data_root / "suites" / "case", worker)
        digest = "a" * 64
        return data_root, suite, digest

    def _invoke(
        self,
        data_root: Path,
        digest: str,
        action: str,
        *,
        decision: str | None = None,
        uid: str = AGENT_RUN_UID,
        suite_path: str = "case/suite.toml",
        suite_sha256: str | None = None,
    ) -> tuple[int, dict[str, str], bytes, str]:
        termination_log = data_root.parent / "termination.json"
        if suite_sha256 is None:
            suite_sha256 = hashlib.sha256(
                (data_root / "suites" / "case" / "suite.toml").read_bytes()
            ).hexdigest()
        argv = [
            "--action",
            action,
            "--agent-run-uid",
            uid,
            "--execution-spec-sha256",
            digest,
            "--suite-sha256",
            suite_sha256,
            "--suite-path",
            suite_path,
            "--data-root",
            str(data_root),
            "--termination-log",
            str(termination_log),
        ]
        if decision is not None:
            argv.extend(["--resume-decision-sha256", decision])
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = controller_step.main(argv)
        payload = termination_log.read_bytes()
        self.assertEqual(stdout.getvalue().encode("utf-8"), payload)
        self.assertEqual(stderr.getvalue(), "")
        self.assertLessEqual(len(payload), MAX_TERMINATION_BYTES)
        self.assertEqual(payload.count(b"\n"), 1)
        result = json.loads(payload)
        self.assertEqual(set(result), EXPECTED_KEYS)
        self.assertTrue(all(isinstance(value, str) for value in result.values()))
        self.assertEqual(result["protocol"], PROTOCOL)
        self.assertNotIn(str(data_root), stdout.getvalue())
        self.assertNotIn(str(termination_log), stdout.getvalue())
        return code, result, payload, stdout.getvalue()

    def test_successful_start_and_verify_emit_stable_bundle_identity(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="c-") as temporary:
            data_root, _, digest = self._layout(temporary, SUCCESS_WORKER)

            code, started, _, _ = self._invoke(data_root, digest, "start")

            self.assertEqual(code, 0)
            self.assertEqual(started["action"], "start")
            self.assertEqual(started["outcome"], "completed")
            self.assertEqual(started["agent_run_uid"], AGENT_RUN_UID)
            self.assertEqual(started["execution_spec_sha256"], digest)
            self.assertRegex(started["run_id"], r"^[0-9a-f]{32}$")
            self.assertRegex(started["bundle_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(started["resume_decision_sha256"], "")
            self.assertEqual(started["error_code"], "")
            bundle = data_root / "runs" / AGENT_RUN_UID / "bundle.json"
            self.assertEqual(
                started["bundle_sha256"],
                hashlib.sha256(bundle.read_bytes()).hexdigest(),
            )

            code, verified, _, _ = self._invoke(data_root, digest, "verify")

            self.assertEqual(code, 0)
            self.assertEqual(verified["outcome"], "verified")
            self.assertEqual(verified["run_id"], started["run_id"])
            self.assertEqual(verified["bundle_sha256"], started["bundle_sha256"])
            self.assertEqual(verified["resume_decision_sha256"], "")
            self.assertEqual(verified["error_code"], "")

    def test_failed_start_requires_bound_approval_then_resume_completes(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="c-") as temporary:
            data_root, _, digest = self._layout(temporary, FAIL_ONCE_WORKER)

            code, waiting, _, _ = self._invoke(data_root, digest, "start")

            self.assertEqual(code, 0)
            self.assertEqual(waiting["outcome"], "awaiting_approval")
            self.assertRegex(waiting["run_id"], r"^[0-9a-f]{32}$")
            self.assertRegex(
                waiting["resume_decision_sha256"],
                r"^[0-9a-f]{64}$",
            )
            self.assertEqual(waiting["bundle_sha256"], "")
            self.assertEqual(waiting["error_code"], "")

            code, resumed, _, _ = self._invoke(
                data_root,
                digest,
                "resume",
                decision=waiting["resume_decision_sha256"],
            )

            self.assertEqual(code, 0)
            self.assertEqual(resumed["outcome"], "completed")
            self.assertEqual(resumed["run_id"], waiting["run_id"])
            self.assertEqual(
                resumed["resume_decision_sha256"],
                waiting["resume_decision_sha256"],
            )
            self.assertRegex(resumed["bundle_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(resumed["error_code"], "")

    def test_resume_without_decision_is_bounded_invalid_request(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="c-") as temporary:
            data_root, _, digest = self._layout(temporary, FAIL_ONCE_WORKER)
            self._invoke(data_root, digest, "start")

            code, blocked, _, _ = self._invoke(data_root, digest, "resume")

            self.assertEqual(code, 10)
            self.assertEqual(blocked["outcome"], "blocked")
            self.assertEqual(blocked["action"], "resume")
            self.assertEqual(blocked["agent_run_uid"], AGENT_RUN_UID)
            self.assertEqual(blocked["execution_spec_sha256"], digest)
            self.assertEqual(blocked["run_id"], "")
            self.assertEqual(blocked["resume_decision_sha256"], "")
            self.assertEqual(blocked["bundle_sha256"], "")
            self.assertEqual(blocked["error_code"], "invalid_request")

    def test_failed_approved_resume_blocks_without_second_decision(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="c-") as temporary:
            data_root, _, digest = self._layout(temporary, ALWAYS_FAIL_WORKER)
            _, waiting, _, _ = self._invoke(data_root, digest, "start")

            code, blocked, _, _ = self._invoke(
                data_root,
                digest,
                "resume",
                decision=waiting["resume_decision_sha256"],
            )

            self.assertEqual(code, 20)
            self.assertEqual(blocked["outcome"], "blocked")
            self.assertEqual(blocked["error_code"], "execution_failed")
            self.assertEqual(blocked["run_id"], "")
            self.assertEqual(blocked["resume_decision_sha256"], "")
            self.assertEqual(blocked["bundle_sha256"], "")

    def test_execution_spec_echo_is_distinct_from_bound_suite_digest(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="c-") as temporary:
            data_root, _, digest = self._layout(temporary, SUCCESS_WORKER)

            code, completed, _, _ = self._invoke(data_root, digest, "start")

            self.assertEqual(code, 0)
            self.assertEqual(completed["outcome"], "completed")
            self.assertEqual(completed["execution_spec_sha256"], digest)
            self.assertNotEqual(
                digest,
                hashlib.sha256(
                    (data_root / "suites" / "case" / "suite.toml").read_bytes()
                ).hexdigest(),
            )

    def test_suite_digest_mismatch_blocks_before_run_or_task_mutation(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="c-") as temporary:
            data_root, suite, digest = self._layout(temporary, SUCCESS_WORKER)
            actual = hashlib.sha256(suite.read_bytes()).hexdigest()
            wrong = ("0" if actual[0] != "0" else "1") + actual[1:]

            code, blocked, _, _ = self._invoke(
                data_root,
                digest,
                "start",
                suite_sha256=wrong,
            )

            self.assertEqual(code, 30)
            self.assertEqual(blocked["outcome"], "blocked")
            self.assertEqual(blocked["error_code"], "evidence_invalid")
            self.assertFalse((data_root / "runs" / AGENT_RUN_UID).exists())
            self.assertFalse(
                (
                    data_root
                    / "suites"
                    / "case"
                    / "workspace"
                    / "results"
                    / "result.txt"
                ).exists()
            )

    def test_malformed_suite_drift_is_always_evidence_invalid(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="c-") as temporary:
            data_root, suite, digest = self._layout(temporary, SUCCESS_WORKER)
            bound_suite_sha256 = hashlib.sha256(suite.read_bytes()).hexdigest()
            code, started, _, _ = self._invoke(
                data_root,
                digest,
                "start",
                suite_sha256=bound_suite_sha256,
            )
            self.assertEqual(code, 0)
            self.assertEqual(started["outcome"], "completed")

            suite.write_bytes(b"version = [not-valid-toml\n")
            code, blocked, _, _ = self._invoke(
                data_root,
                digest,
                "verify",
                suite_sha256=bound_suite_sha256,
            )

            self.assertEqual(code, 30)
            self.assertEqual(blocked["outcome"], "blocked")
            self.assertEqual(blocked["error_code"], "evidence_invalid")
            self.assertEqual(blocked["run_id"], "")
            self.assertEqual(blocked["bundle_sha256"], "")

    def test_controller_rejects_legacy_suite_before_run_creation(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="c-") as temporary:
            data_root, _, digest = self._layout(temporary, SUCCESS_WORKER)
            legacy = write_suite(
                data_root / "suites" / "legacy",
                SUCCESS_WORKER,
            )
            legacy_sha256 = hashlib.sha256(legacy.read_bytes()).hexdigest()

            code, blocked, _, _ = self._invoke(
                data_root,
                digest,
                "start",
                suite_path="legacy/suite.toml",
                suite_sha256=legacy_sha256,
            )

            self.assertEqual(code, 10)
            self.assertEqual(blocked["outcome"], "blocked")
            self.assertEqual(blocked["error_code"], "invalid_request")
            self.assertFalse((data_root / "runs" / AGENT_RUN_UID).exists())

    def test_suite_digest_rejects_noncanonical_sha256(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="c-") as temporary:
            data_root, _, digest = self._layout(temporary, SUCCESS_WORKER)
            for invalid in ("A" * 64, "a" * 63):
                code, blocked, _, _ = self._invoke(
                    data_root,
                    digest,
                    "start",
                    suite_sha256=invalid,
                )
                self.assertEqual(code, 10)
                self.assertEqual(blocked["error_code"], "invalid_request")
                self.assertFalse((data_root / "runs" / AGENT_RUN_UID).exists())

    def test_execution_spec_rejects_noncanonical_sha256(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="c-") as temporary:
            data_root, _, _ = self._layout(temporary, SUCCESS_WORKER)

            for invalid in ("A" * 64, "a" * 63):
                code, blocked, _, _ = self._invoke(
                    data_root,
                    invalid,
                    "start",
                )

                self.assertEqual(code, 10)
                self.assertEqual(blocked["outcome"], "blocked")
                self.assertEqual(blocked["error_code"], "invalid_request")
                self.assertEqual(blocked["execution_spec_sha256"], "")
                self.assertFalse(
                    (data_root / "runs" / AGENT_RUN_UID).exists()
                )

    def test_uid_and_suite_path_reject_ambiguity_without_escape(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="c-") as temporary:
            data_root, _, digest = self._layout(temporary, SUCCESS_WORKER)

            code, bad_uid, _, _ = self._invoke(
                data_root,
                digest,
                "start",
                uid=AGENT_RUN_UID.upper(),
            )
            self.assertEqual(code, 10)
            self.assertEqual(bad_uid["agent_run_uid"], "")
            self.assertEqual(bad_uid["error_code"], "invalid_request")

            code, bad_path, _, _ = self._invoke(
                data_root,
                digest,
                "start",
                suite_path="../case/suite.toml",
            )
            self.assertEqual(code, 10)
            self.assertEqual(bad_path["agent_run_uid"], AGENT_RUN_UID)
            self.assertEqual(bad_path["error_code"], "invalid_request")
            self.assertFalse((data_root / "runs" / AGENT_RUN_UID).exists())

    def test_internal_exception_text_and_paths_are_never_emitted(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="c-") as temporary:
            data_root, _, digest = self._layout(temporary, SUCCESS_WORKER)
            sensitive_detail = "sensitive exception detail"
            with mock.patch.object(
                controller_step,
                "start_run",
                side_effect=RuntimeError(f"{sensitive_detail}: {data_root}"),
            ):
                code, blocked, payload, stdout = self._invoke(
                    data_root,
                    digest,
                    "start",
                )

            self.assertEqual(code, 70)
            self.assertEqual(blocked["outcome"], "blocked")
            self.assertEqual(blocked["error_code"], "internal_error")
            self.assertNotIn(sensitive_detail, stdout)
            self.assertNotIn(sensitive_detail.encode("utf-8"), payload)

    def test_evidence_failure_is_redacted_and_classified(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="c-") as temporary:
            data_root, _, digest = self._layout(temporary, SUCCESS_WORKER)
            _, started, _, _ = self._invoke(data_root, digest, "start")
            bundle = data_root / "runs" / AGENT_RUN_UID / "bundle.json"
            bundle.write_bytes(
                bundle.read_bytes().replace(
                    b'"kind":"benchhandoff-bundle"',
                    b'"kind":"wrong-bundle-kind"',
                )
            )

            code, blocked, payload, _ = self._invoke(data_root, digest, "verify")

            self.assertEqual(code, 30)
            self.assertEqual(blocked["outcome"], "blocked")
            self.assertEqual(blocked["error_code"], "evidence_invalid")
            self.assertEqual(blocked["run_id"], "")
            self.assertNotIn(started["run_id"].encode("ascii"), payload)

    def test_unknown_and_duplicate_flags_do_not_trigger_argparse_output(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="c-") as temporary:
            data_root, _, digest = self._layout(temporary, SUCCESS_WORKER)
            termination = data_root.parent / "termination.json"
            base = [
                "--action",
                "start",
                "--agent-run-uid",
                AGENT_RUN_UID,
                "--execution-spec-sha256",
                digest,
                "--suite-sha256",
                hashlib.sha256((data_root / "suites" / "case" / "suite.toml").read_bytes()).hexdigest(),
                "--suite-path",
                "case/suite.toml",
                "--data-root",
                str(data_root),
                "--termination-log",
                str(termination),
            ]
            missing_suite_digest = list(base)
            option_index = missing_suite_digest.index("--suite-sha256")
            del missing_suite_digest[option_index : option_index + 2]
            for arguments in (
                [*base, "--unknown", "private"],
                [*base, "--action", "verify"],
                missing_suite_digest,
            ):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    code = controller_step.main(arguments)
                result = json.loads(stdout.getvalue())
                self.assertEqual(code, 10)
                self.assertEqual(stderr.getvalue(), "")
                self.assertEqual(result["outcome"], "blocked")
                self.assertEqual(result["error_code"], "invalid_request")
                self.assertLessEqual(
                    len(stdout.getvalue().encode("utf-8")),
                    MAX_TERMINATION_BYTES,
                )


if __name__ == "__main__":
    unittest.main()
