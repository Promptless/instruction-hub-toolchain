"""Shared host-runtime constants, types, errors, and data contracts."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Union

RUNTIME_VERSION = "0.2.5"


RUNTIME_CHANNEL = "stable"


RUNTIME_EXECUTABLE = "promptless-host-runtime"


MANAGED_RUNTIME_ID = "host-runtime"


DEFAULT_WORKER_BASE_URL = "https://pig.promptless.ai"


DEFAULT_DASHBOARD_BASE_URL = "https://app.gopromptless.ai"


HOSTED_ENROLLMENT_START_PATH = "/instruction-hub/enroll/start"


HOSTED_ENROLLMENT_APPROVAL_PATH = "/instruction-hub/enroll"


MANAGED_BEGIN = "# BEGIN PROMPTLESS MANAGED HOST ENROLLMENT"


MANAGED_END = "# END PROMPTLESS MANAGED HOST ENROLLMENT"


MANAGED_RUNTIME_MANIFEST = Path("hub.managed-runtimes.json")


STATE_FILE_NAME = "host-enrollment-state.json"


LAST_STATUS_FILE_NAME = "last-bootstrap-status.json"


DIAGNOSTIC_LOG_FILE_NAME = "host-runtime-diagnostics.jsonl"


LEDGER_FILE_NAME = "host-runtime-ledger.json"


HTTP_TIMEOUT_SECONDS = 10


ENROLLMENT_CALLBACK_DEADLINE_SECONDS = 300


ENROLLMENT_POLL_DEADLINE_SECONDS = 35


COLLECT_DEADLINE_SECONDS = 25.0


MAX_STDIN_BYTES = 1024 * 1024


MAX_TRACE_BATCH_BYTES = 10 * 1024 * 1024


MAX_RECORD_BYTES = 10 * 1024 * 1024


CHUNK_TARGET_BYTES = 4 * 1024 * 1024


MAX_UPLOAD_CHUNKS_PER_BATCH = 200


# Per-request wire budget measured on the encoded body, distinct from the decoded
# MAX_TRACE_BATCH_BYTES: high-entropy content grows ~4/3 under gzip+base64, so raw
# size alone cannot bound a request. The worker's HTTP body limit is configurable
# (256 MiB shipped); bounding here makes collector request size an invariant instead
# of a property of content entropy. CHUNK_TARGET_BYTES * 4/3 must stay well under
# this budget so multi-line ranges never overflow it; only single oversized lines do.
MAX_TRANSPORT_BATCH_BYTES = 10 * 1024 * 1024


TRANSPORT_CHUNK_OVERHEAD_BYTES = 1024


TRANSPORT_BATCH_OVERHEAD_BYTES = 16 * 1024


MAX_DIAGNOSTIC_LOG_BYTES = 512 * 1024


SOURCE_READ_BLOCK_BYTES = 1024 * 1024


IDLE_SESSION_GRACE_SECONDS = 12 * 60 * 60


CLAUDE_DESKTOP_TRACE_DIR_NAMES = ("local-agent-mode-sessions", "claude-code-sessions")


CLAUDE_MANAGED_ENV_MARKER = "PROMPTLESS_MANAGED_HOST_ENROLLMENT"


# Non-OTEL_* env keys earlier managed bootstraps wrote into Claude settings. Cleanup
# removes these plus every OTEL_* key, but only when the managed marker proves the
# telemetry env belongs to Promptless rather than the user.
CLAUDE_MANAGED_LEGACY_ENV_KEYS = (
    "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA",
    "CLAUDE_CODE_ENABLE_TELEMETRY",
    "ENABLE_BETA_TRACING_DETAILED",
    "BETA_TRACING_ENDPOINT",
)


TEST_URL_OVERRIDE_ENV = "PROMPTLESS_HOST_ENROLLMENT_ALLOW_TEST_URL_OVERRIDES"


OPEN_BROWSER_ENV = "PROMPTLESS_HOST_ENROLLMENT_OPEN_BROWSER"


COLLECT_DEADLINE_ENV = "PROMPTLESS_HOST_RUNTIME_COLLECT_DEADLINE_SECONDS"


CALLBACK_STATE_PARAM = "state"


INTERNAL_PROMPTLESS_EMAIL_DOMAIN = "gopromptless.ai"


INTERNAL_PROMPTLESS_WELCOME_SHOWN_AT_KEY = "internal_promptless_welcome_shown_at"


INTERNAL_PROMPTLESS_WELCOME_SHOWN_VERSIONS_KEY = "internal_promptless_welcome_shown_at_by_version"


INTERNAL_PROMPTLESS_WELCOME_MESSAGE_LINES = (
    "            ,-,------,",
    r"          _ \(\(_,--'",
    r"     <`--'\>/(/(__",
    "     /. .  `'` '  \\",
    "    (`')  ,        @",
    "     `-._,        /",
    "        )-)_/--( > ",
    "       ''''  ''''",
    "welcome promptless pigfooder.",
)


# Latch for the one-time "enrollment succeeded" confirmation, keyed by target
# (claude/claude-desktop/codex) so each host is confirmed once. Fired for every user on the first
# healthy enrollment, unlike the internal-only pigfooder welcome above.
FIRST_ENROLLMENT_SUCCESS_SHOWN_KEY = "first_enrollment_success_shown_at_by_target"


Host = Literal["codex", "claude", "claude-desktop"]


HOST_VALUES = ("codex", "claude", "claude-desktop")


ConfigStatus = Literal["blocked", "needs_restart", "configured"]


LifecycleEvent = Literal["session_start", "stop", "session_end", "subagent_stop"]


SourceEventKind = Literal["jsonl_range", "oversized_record"]


# Worker contract vocabulary (NativeTraceOversizedRecordChunk.oversized_reason):
# content_size marks raw records over MAX_RECORD_BYTES; transport_size marks records
# whose gzip+base64 encoding cannot fit one request's transport budget.
OversizedReason = Literal["content_size", "transport_size"]


# This bin runs under the host's own python3, which on macOS is the system Python 3.9
# (/usr/bin/python3). Keep runtime-evaluated unions off the PEP 604 ``X | Y`` form (3.10+): these
# aliases and the isinstance() in _json_value() are evaluated at import/call time, unlike annotations
# (kept lazy by ``from __future__ import annotations``). Use typing.Union / isinstance tuples instead.
JsonScalar = Union[str, int, float, bool, None]


JsonValue = Union[JsonScalar, list["JsonValue"], dict[str, "JsonValue"]]


class BootstrapError(RuntimeError):
    """Expected non-fatal bootstrap failure."""


class BootstrapAuthError(BootstrapError):
    """Host credential was rejected by the worker."""


class CollectDeadlineExceeded(BootstrapError):
    """Optional collection work exceeded the hook-safe runtime budget.

    The deadline meters catch-up work only: the idle root scan and upload
    batches after the first. Hook-subject path collection, the first-run
    baseline inventory, and the first pending batch always run so a hook can
    never be starved of its own transcript by tree-scale work.
    """


@dataclass(frozen=True)
class RuntimeMetadata:
    """Runtime and installed-plugin identity used by host operations."""

    bootstrap_version: str
    toolchain_version: str
    plugin_id: str
    plugin_version: str
    package_id: str
    target: Host


@dataclass(frozen=True)
class HostPolicy:
    """Validated worker policy body used by the host runtime.

    The signed policy still carries an OTLP ``collector`` section for older
    bootstraps; this runtime ignores it entirely.
    """

    policy_version: int
    required_bootstrap_version: str | None
    enabled_hosts: tuple[Host, ...]
    expires_at: dt.datetime


@dataclass(frozen=True)
class EnrollmentContext:
    """Host identity and plugin metadata used by browser enrollment."""

    worker_base_url: str
    dashboard_base_url: str
    deployment_instance_id: str
    metadata: RuntimeMetadata
    host_instance_id: str
    host_label: str
    host_platform: str


@dataclass(frozen=True)
class HostedEnrollmentRoutes:
    """Dashboard routes used by browser enrollment."""

    dashboard_base_url: str
    start_path: str
    approval_path: str


@dataclass(frozen=True)
class HostCredential:
    """Local per-host credential approved through the browser flow."""

    value: str
    credential_id: str | None
    deployment_instance_id: str | None
    is_internal_promptless_user: bool = False


@dataclass(frozen=True)
class EnrollmentAttempt:
    """Result of trying to obtain a host credential."""

    credential: HostCredential | None
    reason: str | None = None


@dataclass(frozen=True)
class EnrollmentSession:
    """Pending browser approval session returned through the local callback."""

    session_id: str
    deployment_instance_id: str
    device_code: str
    poll_url: str
    expires_at: dt.datetime
    poll_interval_seconds: int


@dataclass(frozen=True)
class EnrollmentSessionAttempt:
    """Result of creating or retrieving an enrollment approval session."""

    session: EnrollmentSession | None
    reason: str | None = None


@dataclass(frozen=True)
class ConfigResult:
    """Host config write result."""

    status: ConfigStatus
    needs_restart: bool
    effective_config: dict[str, JsonValue]
    drift_reports: list[dict[str, JsonValue]]


@dataclass(frozen=True)
class HookTraceContext:
    """Trace context supplied by a host lifecycle hook."""

    transcript_path: Path | None
    agent_transcript_path: Path | None
    session_id: str | None
    parent_session_id: str | None
    agent_id: str | None
    agent_type: str | None


@dataclass(frozen=True)
class SourceEvent:
    """A forward-only source-file range or oversized-record marker to upload."""

    kind: SourceEventKind
    path: Path
    path_hash: str
    start_offset: int
    end_offset: int
    byte_count: int
    content_sha256: str | None = None
    content: bytes | None = None
    oversized_reason: OversizedReason | None = None


@dataclass(frozen=True)
class UploadBatch:
    """A native trace upload request plus the source events it acknowledges."""

    request: dict[str, JsonValue]
    events: tuple[SourceEvent, ...]


@dataclass
class SourceLedger:
    """Mutable native-source ledger loaded under the host-runtime lock."""

    path: Path
    is_new: bool
    sources: dict[str, dict[str, JsonValue]]
    host_baselines: set[str] = field(default_factory=set)
    reset_sources: set[str] = field(default_factory=set)
    drift_reports: list[dict[str, JsonValue]] = field(default_factory=list)
