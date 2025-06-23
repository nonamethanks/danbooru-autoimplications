from celery import Celery
from celery.schedules import crontab

from autoimplications import logger
from autoimplications.database import update_database
from autoimplications.exceptions import TooManyBursError
from autoimplications.series import Series

logger = logger.opt(colors=True)
tasks = Celery(  # type: ignore[call-arg]
    broker_url="filesystem://",
    broker_transport_options={
        "data_folder_in": "./data/celery",
        "data_folder_out": "./data/celery",
        "control_folder": "./data/celery",
    },
)


@tasks.on_after_configure.connect  # type: ignore[union-attr]
def setup_periodic_tasks(sender: Celery, **kwargs) -> None:  # noqa: ARG001
    sender.add_periodic_task(crontab(minute="13", hour="13"), send_implications.s(), name="Send implications to danbooru.")


@tasks.task(max_retries=0)
def send_implications() -> None:
    update_database()
    for series in Series.from_config():
        if not series.autopost:
            logger.info(f"Skipping series {series.name} because autopost is not configured.")
            continue

        logger.info(f"Running for series {series.name}")
        try:
            series.scan_and_post()
        except TooManyBursError:
            logger.exception(f"Too many BURs for '{series.name}' in {series.topic_url}. Stopping now. Go approve some!")
