import uuid

import pytest

from tests.fakes import JobHarness
from travelrag.jobs.domain import InvalidTransitionError, JobKind, JobStatus
from travelrag.jobs.dummy import run_dummy_pipeline
from travelrag.jobs.service import JobNotFoundError


async def _no_sleep(_: float) -> None:
    return None


async def test_create_commits_before_enqueue_and_publishes() -> None:
    h = JobHarness()
    job = await h.service.create(uuid.uuid4(), JobKind.DUMMY, {"step_seconds": 0})
    assert h.uow.commits == 1
    assert h.queue.enqueued == [job.id]
    assert [e.status for e in h.publisher.delivered] == [JobStatus.PENDING]


async def test_other_users_cannot_see_job() -> None:
    h = JobHarness()
    job = await h.service.create(uuid.uuid4(), JobKind.DUMMY)
    with pytest.raises(JobNotFoundError):
        await h.service.get_for_user(job.id, uuid.uuid4())


async def test_dummy_pipeline_runs_every_stage_to_done() -> None:
    h = JobHarness()
    user = uuid.uuid4()
    job = await h.service.create(user, JobKind.DUMMY, {"step_seconds": 0})

    await run_dummy_pipeline(h.service, job.id, sleep=_no_sleep)

    statuses = [e.status for e in h.publisher.delivered]
    stages = [e.stage for e in h.publisher.delivered if e.stage]
    assert statuses[0] is JobStatus.PENDING
    assert statuses[-1] is JobStatus.DONE
    assert [s.value for s in dict.fromkeys(stages)] == [
        "probe",
        "asr",
        "ocr",
        "extract",
        "resolve",
        "index",
    ]
    assert (await h.service.get_for_user(job.id, user)).progress == 100


async def test_dummy_pipeline_failure_and_retry() -> None:
    h = JobHarness()
    user = uuid.uuid4()
    job = await h.service.create(user, JobKind.DUMMY, {"step_seconds": 0, "fail_at": "ocr"})

    await run_dummy_pipeline(h.service, job.id, sleep=_no_sleep)
    failed = await h.service.get_for_user(job.id, user)
    assert failed.status is JobStatus.FAILED
    assert failed.error is not None and "ocr" in failed.error

    retried = await h.service.retry(job.id, user)
    assert retried.status is JobStatus.PENDING
    assert h.queue.enqueued == [job.id, job.id]


async def test_dummy_pipeline_review_then_approve() -> None:
    h = JobHarness()
    user = uuid.uuid4()
    job = await h.service.create(user, JobKind.DUMMY, {"step_seconds": 0, "needs_review": True})

    await run_dummy_pipeline(h.service, job.id, sleep=_no_sleep)
    assert (await h.service.get_for_user(job.id, user)).status is JobStatus.NEEDS_REVIEW

    approved = await h.service.approve_review(job.id, user)
    assert approved.status is JobStatus.DONE


async def test_approve_requires_review_state() -> None:
    h = JobHarness()
    user = uuid.uuid4()
    job = await h.service.create(user, JobKind.DUMMY)
    with pytest.raises(InvalidTransitionError):
        await h.service.approve_review(job.id, user)
