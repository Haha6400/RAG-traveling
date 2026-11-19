import uuid
from collections.abc import AsyncIterator, Awaitable, Callable

from travelrag.jobs.domain import TERMINAL_STATUSES, Job, JobEvent
from travelrag.jobs.ports import JobEventSubscriber


def format_sse(event: str, data: str) -> str:
    return f"event: {event}\ndata: {data}\n\n"


HEARTBEAT = ": heartbeat\n\n"


async def job_event_stream(
    job_id: uuid.UUID,
    load_snapshot: Callable[[], Awaitable[Job]],
    subscriber: JobEventSubscriber,
) -> AsyncIterator[str]:
    """Stream a job's progress as SSE until it reaches a terminal state.

    The snapshot is read *after* LISTEN starts, so no event between the two can be lost.
    """
    async with subscriber.subscribe() as events:
        job = await load_snapshot()
        yield format_sse("job", JobEvent.from_job(job).to_json())
        if job.is_terminal:
            return
        async for event in events:
            if event is None:
                yield HEARTBEAT
                continue
            if event.job_id != job_id:
                continue
            yield format_sse("job", event.to_json())
            if event.status in TERMINAL_STATUSES:
                return
