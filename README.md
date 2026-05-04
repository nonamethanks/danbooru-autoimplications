A bot that automatically searches for and submits implications for costume tags to danbooru.

Refer to config.yaml for the configuration.

If you want to run this on your own instance, configure `.env` and then launch with `docker compose up`, see tasks.py for the cronjob.
This bot depends heavily on bigquery to get a list of tags without absolutely hammering the site, so good luck if you don't have BQ configured in your own instance.

One-off run locally:

```bash
uv run main.py -s blue_archive    # dry run
uv run main.py -s blue_archive -p # posts it

```
