# Deploying Nassakh on a VPS (Docker Compose)

One Hostinger KVM 4 server (4 vCPU, 16 GB, 200 GB NVMe, x86_64, Ubuntu 26.04) runs everything except the OCR
models, which run on Runpod Serverless (`OCR_BACKEND=runpod`, D104). Caddy takes `https://nassakh.tech` with a
Let's Encrypt certificate. The Mac stays the development machine (`docs/RUNBOOK.md`); the server builds its own
images (`git pull` and `bash deploy/deploy.sh`).

| Service | What it is | Memory limit |
|---|---|---|
| `caddy` | HTTPS, `/static/*`, `/mcp*` to `mcp`, the rest to `web`. The only ports published: 80, 443 | 256 MB |
| `web` | Django under gunicorn (3 processes × 2 threads) | 2.5 GB |
| `worker` | Celery: ingest, preprocessing, layout, Tesseract, Kraken, exports (`default,layout,export`, 3 processes) | 6 GB |
| `gpu-worker` | Celery queue `gpu`: threads that call Runpod (no GPU here) | 2 GB |
| `mcp` | the MCP server (D108), `/mcp` and the secret URL `/mcp/k/<key>` | 768 MB |
| `postgres` | PostgreSQL 17, volume `pgdata`, no published port | 2 GB |
| `redis` | Redis 7 (AOF), volume `redisdata`, no published port | 512 MB |
| `init` | runs once per deploy: migrations, `collectstatic`, `check --deploy`; then exits | 1 GB |

Limits sum to about 14 GB; they are caps, not reservations (a normal day uses 4 to 6 GB), and the server has a 4 GB
swap file. One image (`Dockerfile`) runs every Python service: Python 3.13, Tesseract (`ara`+`eng`), WeasyPrint's
libraries, Kraken in its own Python 3.11 environment with CPU torch and its model (downloaded at build, checked
against a SHA-256), and **no** PyTorch/transformers (they are imported only by the local OCR engines).

Files: `Dockerfile`, `docker-compose.yml`, `deploy/Caddyfile`, `deploy/.env.production.example`,
`deploy/server-setup.sh`, `deploy/deploy.sh`, `deploy/bootstrap.sh`, `deploy/backup.sh`.

## 1. What you need

- Root or `ubuntu` SSH access to the new server, and its IP address.
- Access to `nassakh.tech`'s DNS.
- The Runpod API key and the endpoint ID of the `nassakh-qari-worker` (`docs/RUNBOOK.md` §18).
- An SMTP account for the confirmation emails (host, port, user, password). Without it nobody can sign up.
- Ten minutes for the setup and 10 to 20 minutes for the first image build.

## 2. DNS

At the DNS provider of `nassakh.tech`, two A records to the server's IPv4 address:

| Type | Name | Value |
|---|---|---|
| A | `@` | the server IP |
| A | `www` | the server IP |

Check from your Mac (it must print the server's IP; wait if it prints nothing). Let's Encrypt needs this to work
before the first deploy:

```sh
dig +short nassakh.tech
dig +short www.nassakh.tech
```

Hostinger's own DNS panel (hPanel → Domains → DNS) is the place if the domain is registered there. Remove any
AAAA record that does not point at this server.

## 3. The code on the server

Log in (the first time: as the user you have, probably `ubuntu`; the SSH port is 22 until you move it):

```sh
ssh ubuntu@<SERVER_IP>
```

Install git and clone into `/opt/nassakh`. The repository is public, so the HTTPS clone needs no key:

```sh
sudo apt-get update && sudo apt-get install -y git
sudo install -d -o ubuntu -g ubuntu /opt/nassakh
git clone https://github.com/alibenmussa/Nassakh.git /opt/nassakh
cd /opt/nassakh
```

*Deploy key instead (SSH clone, for a private fork):* make a key on the server, add the printed public key at
GitHub → the repository → Settings → Deploy keys (read-only), then clone over SSH:

```sh
ssh-keygen -t ed25519 -N "" -f ~/.ssh/nassakh_deploy
cat ~/.ssh/nassakh_deploy.pub
printf 'Host github.com\n  IdentityFile ~/.ssh/nassakh_deploy\n  IdentitiesOnly yes\n' >> ~/.ssh/config
git clone git@github.com:alibenmussa/Nassakh.git /opt/nassakh
```

*No GitHub access at all (how the first deploy of this server was done, before the repository was public):* keep a
bare repository on the server and push to it from the Mac over SSH. `deploy.sh`'s `git pull` then runs against it
without any change:

```sh
# on the server, once
sudo install -d -o ubuntu -g ubuntu /opt/nassakh-git && git init -q --bare /opt/nassakh-git
cd /opt/nassakh && git init -q -b main && git remote add origin /opt/nassakh-git
# on the Mac, once (the key is the one added to the server's ~/.ssh/authorized_keys)
git remote add vps ssh://ubuntu@<SERVER_IP>:22022/opt/nassakh-git
GIT_SSH_COMMAND="ssh -i ~/.ssh/nassakh_vps -o IdentitiesOnly=yes" git push vps main
# on the server, once: take the first commit and track it
cd /opt/nassakh && git pull origin main && git branch --set-upstream-to=origin/main main
```

Every update after that is `git push vps main` on the Mac, then `bash deploy/deploy.sh` on the server.

## 4. Server setup (once)

Installs updates, the firewall (`ufw`: SSH, 80, 443), fail2ban, unattended security upgrades, a 4 GB swap file,
Docker with the Compose plugin and log rotation, and prepares `/opt/nassakh`. It does **not** touch your sshd
configuration: you harden SSH by hand. It reads sshd's ports (and your current session's) and keeps them open in
the firewall as well as `SSH_PORT`, so it cannot shut you out.

```sh
cd /opt/nassakh
sudo env SSH_PORT=22022 bash deploy/server-setup.sh
```

Before it turns the firewall on it prints a warning and waits for Enter. **Keep that session open**, press Enter,
then open a **second** terminal and log in again (`ssh -p 22022 ubuntu@<SERVER_IP>`, or your current port). Only
close the first one when the second works. If it does not: `sudo ufw disable` from the first.

If the server has a firewall of its own (Hostinger hPanel → VPS → Firewall), it must allow the SSH port, 80 and
443 (TCP; 443 UDP for HTTP/3). If the script says a reboot is required, run `sudo reboot` and log in again.

Log out and in once more so your user is in the `docker` group, then check:

```sh
docker compose version
docker ps
```

Nothing else may listen on 80 or 443 (some VPS templates start a web server); this prints nothing when they are free:

```sh
sudo ss -tlnp | grep -E ':(80|443) '
```

## 5. The settings file

```sh
cd /opt/nassakh
cp deploy/.env.production.example deploy/.env.production
chmod 600 deploy/.env.production
sed -i "s|^SECRET_KEY=CHANGE_ME|SECRET_KEY=$(openssl rand -hex 32)|; s|^POSTGRES_PASSWORD=CHANGE_ME|POSTGRES_PASSWORD=$(openssl rand -hex 24)|" deploy/.env.production
nano deploy/.env.production
```

The two `sed` lines made the secret key and the database password. In the editor fill what is left (everything else
has a working value for `nassakh.tech`):

| Variable | Fill in |
|---|---|
| `RUNPOD_API_KEY`, `RUNPOD_ENDPOINT_ID` | from the Runpod console. Without them every page fails at the OCR step |
| `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `DEFAULT_FROM_EMAIL` | your SMTP account (write `$$` for a `$` in a password) |
| `ADMIN_EMAIL`, `ADMIN_PASSWORD` | the first superuser (the email is the login; the password at least 10 characters). Used once, by step 7 |
| `SIGNUP_PAGE_QUOTA` | `0`: a new account has no pages until you add some on «الفوترة» (the default) |

Rules of the file: one `NAME=value` per line, no quotes, comments on their own lines, no `$` in values.
`SECRET_KEY` signs sessions and the clips' links: do not change it later. `grep CHANGE_ME deploy/.env.production`
must print nothing. Never commit this file (it is in `.gitignore`).

## 6. First deploy

```sh
cd /opt/nassakh
bash deploy/deploy.sh
```

It links `.env` to the settings file (Compose reads its variables from there), builds the image (10 to 20 minutes
the first time: Node, the Python packages, Kraken's CPU torch and model), starts everything, runs the migrations
(`init`), and waits until every service is healthy. It ends with `https://nassakh.tech/healthz answers 200: the
site is up.` The certificate is requested when Caddy starts; it takes seconds once the DNS is right.

If it stops with an error, read the lines it printed (the failing service's last logs) and see §15.

## 7. First superuser and data

```sh
bash deploy/bootstrap.sh
```

It creates the role groups, the superuser from `ADMIN_EMAIL`/`ADMIN_PASSWORD`, and builds the search index (empty on
a new server). The password is in the database now: delete it from the file and let the containers forget it:

```sh
sed -i '/^ADMIN_PASSWORD=/d' deploy/.env.production
bash deploy/deploy.sh --no-pull
```

(The email is the login. To make another superuser later: `docker compose exec web python manage.py createsuperuser`.)

*The demo books* (`export_demo_bundle` on the Mac makes a bundle; `docs/challenge/DEMO_DATA.md`): copy the bundle's folder to
the server and import it in a throw-away container that shares the media volume. From your Mac:

```sh
rsync -av -e "ssh -p 22022" ~/nassakh-demo-bundle/ ubuntu@<SERVER_IP>:/home/ubuntu/demo-bundle/
```

On the server (the password is the demo user's; the command's `--help` lists the other options):

```sh
cd /opt/nassakh
docker compose run --rm -v /home/ubuntu/demo-bundle:/bundle:ro web \
    python manage.py import_demo_bundle --bundle /bundle --email demo@example.org --password '<PASSWORD>'
```

Take a Hostinger snapshot before importing real books (§12).

## 8. Check that it works

From your Mac:

```sh
curl -I https://nassakh.tech                      # HTTP/2 302 to /accounts/login/ ; a Strict-Transport-Security header
curl -s https://nassakh.tech/healthz              # {"ok": true}
curl -I http://nassakh.tech                       # 308 to https
curl -I https://www.nassakh.tech                  # 301 to https://nassakh.tech/
curl -i -X POST https://nassakh.tech/mcp -H 'Content-Type: application/json' \
     -H 'Accept: application/json, text/event-stream' -d '{}'     # HTTP/2 401 (no access key)
```

In a browser: `https://nassakh.tech/` shows the login page; sign in with the superuser; `/admin/` opens Django admin;
create a book and watch a page go through the pipeline (the first page after a quiet hour waits for Runpod's cold
start). On the server:

```sh
docker compose ps                                 # every service: healthy (init: exited 0)
docker compose logs --tail 20 worker gpu-worker
```

The MCP server's secret URL (`/mcp/k/<key>`) must never be in a log. After you have used a key once:

```sh
docker compose logs --no-color caddy mcp | grep -cE 'nsk_[A-Za-z0-9_-]{20,}'      # 0
```

## 9. Everyday

```sh
cd /opt/nassakh
docker compose ps                                 # state and health
docker compose logs -f web                        # follow one service (web, worker, gpu-worker, mcp, caddy, postgres, redis, init)
docker compose logs --since 1h worker | grep -i error
docker compose restart worker gpu-worker          # restart one or two services
docker compose exec web python manage.py shell    # Django shell
docker compose exec web python manage.py <command>     # any management command (research_reindex, release_page_runs, ...)
docker compose exec postgres psql -U nassakh nassakh   # the database
docker stats --no-stream                          # memory and CPU per service
```

After editing `deploy/.env.production`, apply it: `bash deploy/deploy.sh --no-pull` (containers read the file when they
are created). The queues: `docker compose exec redis redis-cli llen default` (also `gpu`, `layout`, `export`).

## 10. Updating

On the Mac: commit and push. On the server:

```sh
cd /opt/nassakh
bash deploy/deploy.sh
```

It pulls (fast-forward only), takes a database dump (`backups/predeploy-*.sql.gz`, the last 5 kept), rebuilds what
changed, runs the migrations once, restarts the services that changed and waits for health. Visitors may see a
second of waiting while `web` restarts (Caddy holds requests up to 20 s). A running OCR or export task is not lost: tasks are
acknowledged when they finish, and the worker gets a minute to stop.

## 11. Rollback

Back to an earlier commit (the build reuses cached layers, so it is quick):

```sh
cd /opt/nassakh
git log --oneline -10
git checkout <good-commit>
bash deploy/deploy.sh --no-pull
```

To go forward again: `git checkout main && bash deploy/deploy.sh`. (While a commit is checked out, `bash deploy/deploy.sh`
without `--no-pull` cannot pull.)

The fastest way back to the image that ran before the last deploy (no build; it does not undo migrations):

```sh
docker tag nassakh-app:previous nassakh-app:latest && docker compose up -d --no-build
```

A migration that went wrong is undone by restoring the database dump taken just before it (§14): the
`predeploy-*.sql.gz` of that deploy.

## 12. Backups

`deploy/backup.sh` writes to `/opt/nassakh/backups` (`db-*.sql.gz`: the 7 newest; `media-*.tar.gz`: the 2 newest; every
deploy adds a `predeploy-*.sql.gz`) and runs `manage.py expire_quota` (D106, daily). Install the cron line (the media
archive is made on Sundays; the line can be run again, it replaces itself):

```sh
( crontab -l 2>/dev/null | grep -v 'deploy/backup.sh' ; \
  echo '30 2 * * * bash /opt/nassakh/deploy/backup.sh all >> /opt/nassakh/backups/backup.log 2>&1' ) | crontab -
crontab -l
```

Try each part once now, and look at the sizes (the books' files can be large: `df -h /` and `du -sh backups`):

```sh
bash deploy/backup.sh db && bash deploy/backup.sh quota && bash deploy/backup.sh media
ls -lh backups
```

**Copy the backups off the server** (a backup on the same disk is not a backup). From the Mac, daily or before a
risky change:

```sh
rsync -av -e "ssh -p 22022" ubuntu@<SERVER_IP>:/opt/nassakh/backups/ ~/nassakh-backups/
```

**Hostinger snapshots** (hPanel → VPS → Backups & Snapshots) copy the whole disk: the database, the media volume and
Docker. Take one before the first import of real books and before any risky change; restoring one puts the server
back as it was then, losing everything since. They are crash-consistent (PostgreSQL recovers) but they are not a
substitute for the dumps: a dump restores one database in a minute, and it is on your Mac as well.

### Watchdog

`deploy/watchdog.sh` runs every 5 minutes from cron: it starts a service that is missing or stopped, restarts one that
is running but `unhealthy`, and notes in `backups/watchdog.log` when the public `/healthz` does not answer 200. It is
quiet when everything is fine. Install it once:

```sh
( crontab -l 2>/dev/null | grep -v 'deploy/watchdog.sh' ; echo '*/5 * * * * bash /opt/nassakh/deploy/watchdog.sh' ) | crontab -
```

For an outside view (the server itself down), add the address to a free uptime monitor (UptimeRobot, Better Stack)
that checks `https://nassakh.tech/healthz` every 5 minutes and emails you.

## 13. Changing settings

- Memory limits, ports, the worker's concurrency: `docker-compose.yml`, `deploy/.env.production` (`WORKER_CONCURRENCY`,
  `GPU_THREADS`, `WEB_WORKERS`, `WEB_THREADS`); then `bash deploy/deploy.sh --no-pull`.
- Upload size: Caddy accepts 600 MB (`deploy/Caddyfile`); the app refuses a PDF over 500 MB (`books.forms.MAX_PDF_MB`).
- HSTS: `SECURE_HSTS_SECONDS=3600` (one hour) while testing; once everything is stable raise it to `86400`, later
  `31536000`. A browser that has seen HSTS will refuse plain http for that long.
- SSH port changed later: run `sudo env SSH_PORT=<new> bash deploy/server-setup.sh` again (it adds the port to ufw and
  fail2ban), test a new session, then remove the old port: `sudo ufw status numbered`, `sudo ufw delete <n>`.

## 14. Restore

**The database** (from a dump in `backups/`; replace the file name). Stop what writes, recreate the database, load the
dump, start again:

```sh
cd /opt/nassakh
docker compose stop web worker gpu-worker mcp
docker compose exec -T postgres psql -U nassakh -d postgres \
    -c "DROP DATABASE IF EXISTS nassakh WITH (FORCE)" -c "CREATE DATABASE nassakh OWNER nassakh"
gunzip -c backups/db-2026-10-05-023000.sql.gz | docker compose exec -T postgres psql -U nassakh -d nassakh -v ON_ERROR_STOP=1 -q
bash deploy/deploy.sh --no-pull
```

(The names `nassakh`/`nassakh` are `POSTGRES_USER`/`POSTGRES_DB`; the defaults.) Redis holds only the task queue: pages
that were queued when you restored can be released and run again from the book's page (`release_page_runs --book ID`).

**The books' files** (from `backups/media-*.tar.gz`; it holds a top-level `media/` folder that becomes `/data/media`):

```sh
cd /opt/nassakh
docker compose stop web worker gpu-worker mcp
gunzip -c backups/media-2026-10-04-031500.tar.gz | docker compose run --rm --no-deps -T --entrypoint tar web xf - -C /data
docker compose up -d
```

(Files are written as the app user, so the ownership is right. Existing files with the same name are replaced; files
that are not in the archive stay.)

**A whole new server**: §2 to §6 on the new one, restore the database and the media as above (run `bash deploy/deploy.sh`
once first so the volumes exist), then point the DNS at it.

## 15. Troubleshooting

**The certificate is not issued** (browser warning, or `curl` says the certificate is wrong):
`docker compose logs caddy | grep -iE 'acme|challenge|error' | tail -20`. Usual causes: the A record does not point
here yet (`dig +short nassakh.tech`); port 80 or 443 is blocked (`sudo ufw status`, the provider's firewall) or in
use (`sudo ss -tlnp | grep -E ':(80|443) '`); Let's Encrypt's limit after several failures (five a hour per name:
wait an hour; fix the cause first). A missing `www` record only affects `www`. After fixing: `docker compose restart caddy`.

**502 or 503 from the site**: a service is down or still starting. `docker compose ps`, then
`docker compose logs --tail 80 web`. A web container that keeps restarting usually has a wrong setting in the file
(`docker compose logs init` shows migration errors). Caddy waits up to 20 s for `web` during a deploy; longer means
it did not come up.

**400 Bad Request**: the host is not in `ALLOWED_HOSTS`. **403 on login or any form** (CSRF): `CSRF_TRUSTED_ORIGINS`
must be `https://nassakh.tech`, and `BEHIND_PROXY=true`. **Redirect loop**: `SECURE_SSL_REDIRECT=true` needs
`BEHIND_PROXY=true`.

**"SECRET_KEY is empty or still a placeholder"** in a container's log: fill it in `deploy/.env.production`, then
`bash deploy/deploy.sh --no-pull`. **"POSTGRES_PASSWORD is not set"** from `docker compose`: the `.env` link is missing:
`cd /opt/nassakh && ln -sfn deploy/.env.production .env`.

**A worker does not pick up tasks** (pages stay queued): `docker compose ps` (health), `docker compose logs --tail 50 worker`
(or `gpu-worker`), `docker compose exec worker celery -A nassakh inspect active`. If the queue is long and the worker idle:
`docker compose restart worker gpu-worker`. The OCR step waits when Runpod has no GPU (it requeues the page by itself,
`docs/RUNBOOK.md` §18); a page stuck after a restart is released with
`docker compose exec web python manage.py release_page_runs --book ID`. `Redis` must be healthy.

**Out of memory** (a service restarts, `docker inspect -f '{{.State.OOMKilled}}' <container>` is true, or
`dmesg | grep -i 'out of memory'`): `docker stats --no-stream` and `free -h` show who. Lower `WORKER_CONCURRENCY` to 2 in
the file (an export spikes to 2 GB and Kraken takes 1.5 GB per page batch), or raise that service's `mem_limit` in
`docker-compose.yml`, then `bash deploy/deploy.sh --no-pull`. The swap file is the safety net, not capacity.

**Disk full**: `df -h /`, `docker system df`. Free build leftovers with `docker builder prune -f` and
`docker image prune -f`; old backups in `backups/`; the books' files are in the `nassakh_media` volume.

**`permission denied` talking to docker**: log out and in (the docker group). **A fonts warning** («الخط غير مثبّت»):
Simplified Arabic, Traditional Arabic, Times New Roman and Lotus are not on the server, so a book styled with them
exports in Amiri; upload the faces on the organisation's page (D98) to use them.

**Emails do not arrive**: `docker compose logs web | grep -i 'confirmation email'` names the SMTP error;
`docker compose exec web python manage.py sendtestemail you@example.com` tries it.

**Never run `docker compose down -v`** (or `docker volume rm`): the volumes hold the database and every book.
`docker compose down` and `up` are safe.

### OCR pages take minutes instead of seconds

Measured on the first deploy (4 vCPU): a Tesseract page took **340 s on average, up to 615 s**, and the pipeline
crawled at 2 pages in 8 minutes. Cause: each Tesseract and Kraken (torch) process starts one OpenMP thread per core,
and three or four of them at once on 4 vCPUs spin against each other. `docker-compose.yml` therefore sets
`OMP_THREAD_LIMIT=1` and `OMP_NUM_THREADS=1` for every app service; with that a page takes about 1.3 s, and the
parallelism comes from the worker processes. If pages are slow again, check `docker stats` (a worker at 400 % CPU
with a long `ocr_page_fast` in `docker compose logs worker`) and that both variables are set in the container
(`docker compose exec worker env | grep OMP`).

The GPU worker also runs Kraken for the word boxes (about 1.5 GB per reading thread), which is why it has a 6 GB
limit and `GPU_THREADS=3`. A killed Kraken shows as `Killed` in `docker compose logs gpu-worker`.

## 16. What is exposed

Only Caddy publishes ports (80, 443); PostgreSQL, Redis, gunicorn and the MCP server are on the compose network and
unreachable from outside (Docker publishes around `ufw`, which is why nothing else is published). Media is served by
Django only (`/media/...` checks who may see each file); Caddy serves `/static/*` only. Caddy never logs the MCP
secret URL (`deploy/Caddyfile` explains how: `log_skip` for the access log, a filter on the error log). The containers
run as an unprivileged user (uid 1000); `init` starts as root only to make the volumes writable.
