"""Job entity and its state machine. Pure Python: no framework or database imports."""

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Self


class JobStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    NEEDS_REVIEW = "needs_review"
    DONE = "done"
    FAILED = "failed"


class JobStage(StrEnum):
    """Stages of the video pipeline, in execution order."""

    PROBE = "probe"
    ASR = "asr"
    OCR = "ocr"
    EXTRACT = "extract"
    RESOLVE = "resolve"
    AGGREGATE = "aggregate"
    INDEX = "index"


class JobKind(StrEnum):
    DUMMY = "dummy"
    PROCESS_VIDEO = "process_video"


TERMINAL_STATUSES = frozenset({JobStatus.DONE, JobStatus.FAILED})

_ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.PENDING: frozenset({JobStatus.PROCESSING}),
    JobStatus.PROCESSING: frozenset({JobStatus.NEEDS_REVIEW, JobStatus.DONE, JobStatus.FAILED}),
    JobStatus.NEEDS_REVIEW: frozenset({JobStatus.PROCESSING, JobStatus.DONE, JobStatus.FAILED}),
    JobStatus.DONE: frozenset(),
    JobStatus.FAILED: frozenset({JobStatus.PENDING}),
}


class InvalidTransitionError(Exception):
    def __init__(self, current: JobStatus, target: JobStatus) -> None:
        super().__init__(f"cannot move job from '{current}' to '{target}'")
        self.current = current
        self.target = target


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class Job:
    id: uuid.UUID
    user_id: uuid.UUID
    kind: JobKind
    status: JobStatus = JobStatus.PENDING
    stage: JobStage | None = None
    progress: int = 0
    error: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    video_id: uuid.UUID | None = None
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)

    @classmethod
    def new(
        cls,
        user_id: uuid.UUID,
        kind: JobKind,
        payload: dict[str, Any] | None = None,
        video_id: uuid.UUID | None = None,
    ) -> Self:
        return cls(
            id=uuid.uuid4(), user_id=user_id, kind=kind, payload=payload or {}, video_id=video_id
        )

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def _move(self, target: JobStatus) -> None:
        if target not in _ALLOWED_TRANSITIONS[self.status]:
            raise InvalidTransitionError(self.status, target)
        self.status = target
        self.updated_at = _now()

    def start(self) -> None:
        self._move(JobStatus.PROCESSING)
        self.error = None

    def advance(self, stage: JobStage, progress: int) -> None:
        if self.status is not JobStatus.PROCESSING:
            raise InvalidTransitionError(self.status, JobStatus.PROCESSING)
        if not 0 <= progress <= 100:
            raise ValueError(f"progress must be within 0..100, got {progress}")
        self.stage = stage
        self.progress = progress
        self.updated_at = _now()

    def request_review(self) -> None:
        self._move(JobStatus.NEEDS_REVIEW)

    def complete(self) -> None:
        self._move(JobStatus.DONE)
        self.progress = 100

    def fail(self, error: str) -> None:
        self._move(JobStatus.FAILED)
        self.error = error

    def retry(self) -> None:
        self._move(JobStatus.PENDING)
        self.stage = None
        self.progress = 0
        self.error = None


@dataclass(frozen=True)
class JobEvent:
    """Snapshot of a job's progress, pushed to clients."""

    job_id: uuid.UUID
    status: JobStatus
    stage: JobStage | None
    progress: int
    error: str | None
    updated_at: datetime

    @classmethod
    def from_job(cls, job: Job) -> Self:
        return cls(
            job_id=job.id,
            status=job.status,
            stage=job.stage,
            progress=job.progress,
            error=job.error,
            updated_at=job.updated_at,
        )

    def to_json(self) -> str:
        return json.dumps(
            {
                "job_id": str(self.job_id),
                "status": self.status.value,
                "stage": self.stage.value if self.stage else None,
                "progress": self.progress,
                "error": self.error,
                "updated_at": self.updated_at.isoformat(),
            }
        )

    @classmethod
    def from_json(cls, raw: str) -> Self:
        data = json.loads(raw)
        return cls(
            job_id=uuid.UUID(data["job_id"]),
            status=JobStatus(data["status"]),
            stage=JobStage(data["stage"]) if data["stage"] else None,
            progress=int(data["progress"]),
            error=data["error"],
            updated_at=datetime.fromisoformat(data["updated_at"]),
        )
