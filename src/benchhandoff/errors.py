"""Typed failures and stable command-line exit codes."""


class BenchHandoffError(Exception):
    """Base class for an expected, user-actionable failure."""

    exit_code = 10


class ConfigurationError(BenchHandoffError):
    """The suite or command-line configuration is invalid."""


class BoundaryError(BenchHandoffError):
    """A path crossed a declared boundary or was not a regular file."""


class TaskExecutionError(BenchHandoffError):
    """A benchmark task failed closed."""

    exit_code = 20


class EvidenceError(BenchHandoffError):
    """Recorded evidence is missing, inconsistent, or has drifted."""

    exit_code = 30
