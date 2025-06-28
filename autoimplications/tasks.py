from celery import Celery
from celery.schedules import crontab
from celery_once import QueueOnce

from autoimplications import logger
from autoimplications.database import update_database
from autoimplications.exceptions import TooManyBursError
from autoimplications.series import Series

logger = logger.opt(colors=True)
tasks = Celery(  # type: ignore[call-arg]
    broker_url="filesystem://",
    broker_transport_options={
        "data_folder_in": "/tmp/celery",
        "data_folder_out": "/tmp/celery",
        "control_folder": "/tmp/celery",
        "visibility_timeout": "3600",
    },
)

tasks.conf.ONCE = {
    "backend": "celery_once.backends.File",
    "settings": {
        "location": "/tmp/celery/celery_once",
        "default_timeout": 3600,
    },
}


@tasks.on_after_configure.connect  # type: ignore[union-attr]
def setup_periodic_tasks(sender: Celery, **kwargs) -> None:  # noqa: ARG001
    sender.add_periodic_task(crontab(minute="13", hour="13"), send_implications.s(), name="Send implications to danbooru.")

    # TODO: send me a daily email with a summary of what was done at the end


@tasks.task(base=QueueOnce, max_retries=0)
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
