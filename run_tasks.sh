#!/bin/bash
uv run celery -A autoimplications.tasks worker -E -B --loglevel=INFO --concurrency=1
