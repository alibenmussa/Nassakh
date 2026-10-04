web: .venv/bin/python manage.py runserver 8000
worker: .venv/bin/celery -A nassakh worker -Q default,layout,export -c 4 -l info
gpu-worker: make gpu-worker
mcp: .venv/bin/python manage.py mcp_serve
