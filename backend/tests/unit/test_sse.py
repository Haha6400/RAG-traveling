import json
import uuid

from tests.fakes import FakeSubscriber
from travelrag.api.sse import HEARTBEAT, job_event_stream
from travelrag.jobs.domain import Job, JobEvent, JobKind, JobStage


def _event_for(job: Job) -> JobEvent:
    return JobEvent.from_job(job)


async def _collect(job: Job, events: list[JobEvent | None]) -> list[str]:
    subscriber = FakeSubscriber(events)

    async def load() -> Job:
        assert subscriber.listening, "snapshot must be read after LISTEN starts"
        return job

    return [chunk async for chunk in job_event_stream(job.id, load, subscriber)]


async def test_stream_sends_snapshot_then_own_events_until_terminal() -> None:
    job = Job.new(uuid.uuid4(), JobKind.DUMMY)
    other = Job.new(uuid.uuid4(), JobKind.DUMMY)

    progressed = Job.new(job.user_id, JobKind.DUMMY)
    progressed.id = job.id
    progressed.start()
    progressed.advance(JobStage.ASR, 0)
    done = Job.new(job.user_id, JobKind.DUMMY)
    done.id = job.id
    done.start()
    done.complete()

    chunks = await _collect(
        job,
        [_event_for(other), None, _event_for(progressed), _event_for(done), _event_for(progressed)],
    )

    data = [json.loads(c.split("data: ", 1)[1]) for c in chunks if c.startswith("event:")]
    assert [d["status"] for d in data] == ["pending", "processing", "done"]
    assert {d["job_id"] for d in data} == {str(job.id)}
    assert HEARTBEAT in chunks


async def test_stream_stops_immediately_for_terminal_job() -> None:
    job = Job.new(uuid.uuid4(), JobKind.DUMMY)
    job.start()
    job.fail("x")
    chunks = await _collect(job, [None, None])
    assert len(chunks) == 1
