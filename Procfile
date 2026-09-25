web: .venv/bin/python manage.py runserver 8000
worker: .venv/bin/celery -A nassakh worker -Q default,layout -c 4 -l info
gpu-worker: .venv/bin/celery -A nassakh worker -Q gpu -c 1 -P solo -l info
