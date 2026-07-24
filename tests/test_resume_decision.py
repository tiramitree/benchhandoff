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

import benchhandoff.engine as engine
from benchhandoff.cli import main as cli_main
from benchhandoff.engine import inspect_resume, resume_run, start_run, verify_run
from benchhandoff.errors import EvidenceError
from benchhandoff.storage import canonical_json_bytes
from tests.test_benchhandoff import FAIL_ONCE_WORKER, write_suite
from tests.workspace_temp import WorkspaceTemporaryDirectory


def _tree_snapshot(*roots: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            key = f"{root.name}/{path.relative_to(root).as_posix()}"
            snapshot[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    return snapshot


def _attempt_count(run: Path) -> int:
    state = json.loads((run / "state.json").read_text(encoding="utf-8"))
    return len(state["tasks"]["one"]["attempts"])


class ResumeDecisionTests(unittest.TestCase):
    def _failed_run(self, root: Path) -> tuple[Path, Path]:
        root.mkdir(parents=True, exist_ok=True)
        suite = write_suite(root / "suite", FAIL_ONCE_WORKER)
        run = root / "run"
        self.assertEqual(start_run(suite, run).status, "failed")
        return suite, run

    def test_inspect_is_deterministic_and_read_only(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="resume-decision-readonly-") as temporary:
            root = Path(temporary)
            suite, run = self._failed_run(root)
            before = _tree_snapshot(suite.parent, run)

            first = inspect_resume(run)
            second = inspect_resume(run)

            self.assertEqual(first, second)
            self.assertEqual(before, _tree_snapshot(suite.parent, run))
            self.assertEqual(first["kind"], "benchhandoff-resume-decision")
            self.assertEqual(first["action"], "recover-and-resume")
            self.assertEqual(len(first["decision_sha256"]), 64)
            decision_body = dict(first)
            recorded_digest = decision_body.pop("decision_sha256")
            self.assertEqual(
                recorded_digest,
                hashlib.sha256(canonical_json_bytes(decision_body)).hexdigest(),
            )
            self.assertEqual(first["next_task"]["current_status"], "failed")
            self.assertEqual(
                first["next_task"]["unverified_outputs"][0]["status"],
                "present",
            )

    def test_current_decision_allows_bound_resume(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="resume-decision-current-") as temporary:
            root = Path(temporary)
            _, run = self._failed_run(root)
            decision = inspect_resume(run)

            result = resume_run(
                run,
                expected_decision_sha256=decision["decision_sha256"],
            )

            self.assertEqual(result.status, "completed")
            self.assertEqual(verify_run(run)["status"], "verified")
            self.assertEqual(_attempt_count(run), 2)

    def test_partial_output_drift_rejects_without_mutation_then_refreshes(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="resume-decision-output-") as temporary:
            root = Path(temporary)
            suite, run = self._failed_run(root)
            stale = inspect_resume(run)
            partial = suite.parent / "result.txt"
            partial.write_text("reviewed bytes changed", encoding="utf-8")
            before_rejection = _tree_snapshot(run)
            attempts_before = _attempt_count(run)

            with self.assertRaisesRegex(EvidenceError, "resume decision is stale"):
                resume_run(
                    run,
                    expected_decision_sha256=stale["decision_sha256"],
                )

            self.assertEqual(before_rejection, _tree_snapshot(run))
            self.assertEqual(attempts_before, _attempt_count(run))
            self.assertEqual(partial.read_text(encoding="utf-8"), "reviewed bytes changed")
            self.assertEqual(list((run / "quarantine").iterdir()), [])

            refreshed = inspect_resume(run)
            self.assertNotEqual(
                stale["decision_sha256"],
                refreshed["decision_sha256"],
            )
            result = resume_run(
                run,
                expected_decision_sha256=refreshed["decision_sha256"],
            )
            self.assertEqual(result.status, "completed")
            self.assertEqual(_attempt_count(run), 2)

    def test_attempt_log_drift_rejects_before_state_or_child_mutation(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="resume-decision-log-") as temporary:
            root = Path(temporary)
            _, run = self._failed_run(root)
            decision = inspect_resume(run)
            stderr_log = run / "logs" / "one" / "attempt-0001.stderr.log"
            stderr_log.write_bytes(stderr_log.read_bytes() + b"drift\n")
            before_rejection = _tree_snapshot(run)

            with self.assertRaisesRegex(EvidenceError, "resume decision is stale"):
                resume_run(
                    run,
                    expected_decision_sha256=decision["decision_sha256"],
                )

            self.assertEqual(before_rejection, _tree_snapshot(run))
            self.assertEqual(_attempt_count(run), 1)
            self.assertEqual(list((run / "quarantine").iterdir()), [])

    def test_invalid_or_foreign_decision_rejects_without_mutation(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="resume-decision-foreign-") as temporary:
            root = Path(temporary)
            _, first_run = self._failed_run(root / "first")
            _, second_run = self._failed_run(root / "second")
            foreign = inspect_resume(first_run)["decision_sha256"]
            before = _tree_snapshot(second_run)

            with self.assertRaisesRegex(EvidenceError, "64 lowercase hexadecimal"):
                resume_run(second_run, expected_decision_sha256="A" * 64)
            with self.assertRaisesRegex(EvidenceError, "resume decision is stale"):
                resume_run(second_run, expected_decision_sha256=foreign)

            self.assertEqual(before, _tree_snapshot(second_run))
            self.assertEqual(_attempt_count(second_run), 1)

    def test_completed_decision_and_bound_resume_are_read_only(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="resume-decision-complete-") as temporary:
            root = Path(temporary)
            _, run = self._failed_run(root)
            first = inspect_resume(run)
            self.assertEqual(
                resume_run(
                    run,
                    expected_decision_sha256=first["decision_sha256"],
                ).status,
                "completed",
            )
            decision = inspect_resume(run)
            before = _tree_snapshot(run)

            result = resume_run(
                run,
                expected_decision_sha256=decision["decision_sha256"],
            )

            self.assertEqual(result.status, "completed")
            self.assertEqual(decision["action"], "already-complete")
            self.assertEqual(before, _tree_snapshot(run))

    def test_pending_transition_inspection_refuses_without_reconciliation(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="resume-decision-pending-") as temporary:
            root = Path(temporary)
            _, run = self._failed_run(root)
            context = engine._load_context(run)
            context.state["status"] = "running"
            context.state["last_error"] = None
            with mock.patch.object(
                engine,
                "atomic_write_bytes",
                side_effect=OSError("synthetic event write failure"),
            ):
                with self.assertRaises(OSError):
                    engine._commit_transition(
                        context,
                        "run_resumed",
                        details={"previous_status": "failed"},
                    )
            before = _tree_snapshot(run)

            with self.assertRaisesRegex(EvidenceError, "requires stable event/state"):
                inspect_resume(run)

            self.assertEqual(before, _tree_snapshot(run))

    def test_second_decision_check_blocks_pretransition_drift(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="resume-decision-double-check-") as temporary:
            root = Path(temporary)
            _, run = self._failed_run(root)
            expected = "0" * 64
            before = _tree_snapshot(run)
            with mock.patch.object(
                engine,
                "_build_resume_decision",
                side_effect=[
                    {"decision_sha256": expected},
                    {"decision_sha256": "1" * 64},
                ],
            ):
                with self.assertRaisesRegex(EvidenceError, "became stale"):
                    resume_run(run, expected_decision_sha256=expected)

            self.assertEqual(before, _tree_snapshot(run))
            self.assertEqual(_attempt_count(run), 1)
    def test_cli_inspect_and_bound_resume(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="resume-decision-cli-") as temporary:
            root = Path(temporary)
            _, run = self._failed_run(root)
            with mock.patch("benchhandoff.cli._emit") as emit:
                self.assertEqual(cli_main(["inspect", str(run)]), 0)
            decision = emit.call_args.args[0]

            with mock.patch("benchhandoff.cli._emit") as emit:
                code = cli_main(
                    [
                        "resume",
                        str(run),
                        "--expected-decision-sha256",
                        decision["decision_sha256"],
                    ]
                )
            self.assertEqual(code, 0)
            self.assertEqual(emit.call_args.args[0]["status"], "completed")

    def test_cli_stale_decision_returns_evidence_error_code(self) -> None:
        with WorkspaceTemporaryDirectory(prefix="resume-decision-cli-stale-") as temporary:
            root = Path(temporary)
            suite, run = self._failed_run(root)
            decision = inspect_resume(run)
            (suite.parent / "result.txt").write_text("drift", encoding="utf-8")
            before = _tree_snapshot(run)
            errors = io.StringIO()

            with contextlib.redirect_stderr(errors):
                code = cli_main(
                    [
                        "resume",
                        str(run),
                        "--expected-decision-sha256",
                        decision["decision_sha256"],
                    ]
                )

            error = json.loads(errors.getvalue())
            self.assertEqual(code, 30)
            self.assertEqual(error["error_type"], "EvidenceError")
            self.assertIn("resume decision is stale", error["detail"])
            self.assertEqual(before, _tree_snapshot(run))
            self.assertEqual(_attempt_count(run), 1)


if __name__ == "__main__":
    unittest.main()
