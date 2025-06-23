A bot that automatically searches for and submits implications for costume tags to danbooru.

Refer to config.yaml for the configuration.

If you want to run this on your own instance, configure `.env` and then launch with `docker compose up`, see tasks.py for the cronjob.
This bot depends heavily on bigquery to get a list of tags without absolutely hammering the site, so good luck if you don't have BQ configured in your own instance.

One-off run with celery:
```bash
CELERY_COMMAND="uv run celery -A autoimplications.tasks call autoimplications.tasks.send_implications"
DOCKER_CONTAINER=$(docker ps | grep autoimplications | awk '{print $1}')
docker exec -it "$DOCKER_CONTAINER" sh -c "$CELERY_COMMAND" && docker compose logs -f

```
