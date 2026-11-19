import procrastinate

from travelrag.core.config import get_settings

app = procrastinate.App(
    connector=procrastinate.PsycopgConnector(conninfo=get_settings().psycopg_conninfo),
    import_paths=["travelrag.worker.tasks"],
)
