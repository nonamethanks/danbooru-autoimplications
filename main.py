from __future__ import annotations

import click
from loguru import logger

from autoimplications.database import update_database
from autoimplications.exceptions import TooManyBursError
from autoimplications.series import Series

logger = logger.opt(colors=True)


@click.command()
@click.option("-s", "--series", nargs=1)
@click.option("-m", "--max-lines-per-bur", nargs=1, default=1, type=click.IntRange(1, 100))
@click.option("-p", "--post_to_danbooru", is_flag=True, default=False)
@click.option("-g", "--grep", nargs=1)
def main(series: str | None = None,
         max_lines_per_bur: int = 1,
         post_to_danbooru: bool = False,
         grep: str | None = None) -> None:

    update_database()

    if series:
        logger.info(f"<r>Running only for series {series}.</r>")

    found = False
    for config_series in Series.from_config(grep=grep):
        if series and not config_series.matches(series):
            continue

        config_series.autopost = post_to_danbooru
        try:
            config_series.scan_and_post(max_lines_per_bur=max_lines_per_bur)
        except TooManyBursError:
            logger.error(f"Too many BURs for '{config_series.name}' in {config_series.topic_url}. Stopping now. Go approve some!")  # noqa: TRY400
        found = True

    if series and not found:
        raise ValueError(f"Series '{series}' not found in config.")


if __name__ == "__main__":
    main()
