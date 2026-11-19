import uuid

import pytest

from travelrag.jobs.domain import (
    InvalidTransitionError,
    Job,
    JobEvent,
    JobKind,
    JobStage,
    JobStatus,
)


def make_job() -> Job:
    return Job.new(uuid.uuid4(), JobKind.DUMMY)


def test_happy_path_reaches_done_with_full_progress() -> None:
    job = make_job()
    job.start()
    job.advance(JobStage.ASR, 20)
    job.complete()
    assert job.status is JobStatus.DONE
    assert job.progress == 100
    assert job.is_terminal


def test_review_then_done() -> None:
    job = make_job()
    job.start()
    job.request_review()
    assert job.status is JobStatus.NEEDS_REVIEW
    assert not job.is_terminal
    job.complete()
    assert job.status is JobStatus.DONE


def test_fail_then_retry_resets_progress() -> None:
    job = make_job()
    job.start()
    job.advance(JobStage.OCR, 40)
    job.fail("boom")
    assert job.error == "boom"
    job.retry()
    assert (job.status, job.stage, job.progress, job.error) == (JobStatus.PENDING, None, 0, None)


@pytest.mark.parametrize(
    "action",
    [
        lambda j: j.complete(),  # pending -> done
        lambda j: j.request_review(),  # pending -> needs_review
        lambda j: j.retry(),  # pending -> pending
        lambda j: j.advance(JobStage.ASR, 10),  # advancing while not processing
    ],
)
def test_illegal_moves_from_pending(action) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(InvalidTransitionError):
        action(make_job())


def test_done_is_final() -> None:
    job = make_job()
    job.start()
    job.complete()
    with pytest.raises(InvalidTransitionError):
        job.start()


def test_progress_bounds() -> None:
    job = make_job()
    job.start()
    with pytest.raises(ValueError):
        job.advance(JobStage.ASR, 101)


def test_event_json_roundtrip() -> None:
    job = make_job()
    job.start()
    job.advance(JobStage.EXTRACT, 60)
    event = JobEvent.from_job(job)
    assert JobEvent.from_json(event.to_json()) == event
