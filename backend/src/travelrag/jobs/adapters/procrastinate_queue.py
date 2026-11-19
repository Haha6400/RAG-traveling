import procrastinate

from travelrag.jobs.domain import Job, JobKind

# Task names registered by travelrag.worker.tasks. Deferring by name keeps the API from
# importing worker code.
TASK_NAMES: dict[JobKind, str] = {
    JobKind.DUMMY: "jobs.dummy",
}


class ProcrastinateJobQueue:
    def __init__(self, app: procrastinate.App) -> None:
        self._app = app

    async def enqueue(self, job: Job) -> None:
        await self._app.configure_task(TASK_NAMES[job.kind]).defer_async(job_id=str(job.id))
