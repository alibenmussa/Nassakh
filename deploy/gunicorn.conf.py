"""gunicorn for the `web` service (docker-compose.yml). Numbers come from deploy/.env.production."""

import os

bind = "0.0.0.0:8000"
# 3 workers x 2 threads: 2-3 people at a time; each worker is a Django process of its own (~300 MB)
workers = int(os.environ.get("WEB_WORKERS", "3"))
threads = int(os.environ.get("WEB_THREADS", "2"))
worker_class = "gthread"
# A worker is killed only when it stops answering its master (not for a long request): uploads and
# downloads of big files are fine. Exports and OCR run in Celery, not here.
timeout = int(os.environ.get("WEB_TIMEOUT", "300"))
graceful_timeout = 30
keepalive = 5
# a worker restarts itself now and then (a cheap guard against a slow memory creep)
max_requests = 1000
max_requests_jitter = 100
# only Caddy can reach this port (the compose network): its X-Forwarded-Proto is the truth
forwarded_allow_ips = "*"
worker_tmp_dir = "/dev/shm"
# logs to the container's stdout / stderr (`docker compose logs web`); the client is Caddy's X-Forwarded-For
accesslog = "-"
errorlog = "-"
access_log_format = '%({x-forwarded-for}i)s "%(r)s" %(s)s %(b)s %(M)sms'
