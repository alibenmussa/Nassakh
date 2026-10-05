# Production deployment report: nassakh.tech (5 Oct 2026)

Written after the first deploy and a full test of the user journey on the live server. The step-by-step runbook is
`docs/DEPLOY.md`; this file says what was done, what was measured, what broke and what is still the owner's.

## 1. State now

| | |
|---|---|
| Address | https://nassakh.tech (also `www`, redirected to the bare domain), Let's Encrypt certificate valid to 3 Jan 2027, HTTP/3 on |
| Server | Hostinger KVM 4: 4 vCPU, 15 GB RAM, 193 GB disk (11 % used), Ubuntu 26.04.1, x86_64, Docker 29.8.2, Compose 5.6.0, IP 187.7.65.46 |
| Stack (`/opt/nassakh`) | `caddy`, `web` (gunicorn 3×2), `worker` (cpu, 3 processes), `gpu-worker` (RunPod, 3 threads), `mcp`, `postgres:17`, `redis:7`; `init` runs migrations once per deploy. All healthy. |
| OCR | `OCR_BACKEND=runpod`, endpoint worker 0.1.2 (RTX PRO 6000 MIG 24 GB tier), Kraken and Tesseract run on the server's CPU |
| Mail | **console backend**: confirmation emails are printed in `docker compose logs web`. Not sent. See §6. |
| Access | SSH key only, port 22022, no root login, no passwords; ufw allows 22022, 80, 443; fail2ban on sshd |
| Code | deployed commit is the newest on the `vps` remote (a private bare repo on the server; GitHub is not involved yet) |
| Backups | nightly 02:30 `deploy/backup.sh all` (database dump, 7 kept, weekly media archive); a dump is also taken before every deploy |
| Credentials | `~/nassakh-production-credentials.txt` on the Mac (owner-only): superuser, demo account, the two test users |

## 2. Accounts on the server

- Superuser `alibenmussa@gmail.com` (login is by email only, any case).
- **Demo account** «حساب التجربة» (`demo@nassakh.tech`, unlimited) with the three books over 100 pages: «ولاة طرابلس»
  (294 p), «مختصر صحيح البخاري» (120 p), «تاريخ ليبيا العام» (196 p). Imported from the bundle made on the Mac
  (`docs/DEMO_DATA.md`), manuscripts re-assembled first so the dashboard shows no stale pages.
- **Test users made by the journey test** (delete them in Django admin before judging if you want a clean server):
  `tester1@nassakh.tech` (individual, 6 books of Islamic texts, plan 1 granted by the superuser) and
  `tester2@nassakh.tech` (organisation «دار الوراق للنشر», quota 0).

## 3. What was tested on the live server (all over HTTPS)

| Check | Result |
|---|---|
| `/healthz`, login, sign-up pages; `/books/` redirects when signed out; `http` → `https` (308) | pass |
| `POST /mcp` without a key | 401 |
| Static files through Caddy | pass |
| Sign-up as **individual** and as **organisation**; confirmation link from the log; automatic sign-in; sidebar «الرصيد 0 صفحة» | pass |
| Same email in another case refused | pass |
| Upload with quota 0 | refused: «هذا الكتاب نحو 12 صفحة ورصيدك المتاح 0 صفحة.» nothing saved |
| Superuser grants a plan on «الفوترة» | pass; sidebar shows 1500 |
| Upload of 6 PDFs (133 pages) → extraction and layout | 104 s for all six together |
| Model reading on RunPod | 114 of 132 pages read, **0 failed GPU calls of 238**, 0 pages in error |
| Quota accounting | 1500 granted − 114 charged = 1386 balance, 18 held for unread pages: matches the ledger |
| Quality of reading | الورقات read cleanly (definitions, punctuation); a typeset PDF page of «إيقاظ الهمم» shows vowels, a verse block and a footnote correctly |
| Search and quote check on books uploaded minutes earlier | exact → `exact`; one word changed → `differs` with the word located; absent → `not_found` |
| Isolation | the demo account sees none of the tester's text (0 hits) and gets 404 on their books, text API and media |
| Normal user on `/accounts/billing/` and `/admin/` | 404 and a redirect to the admin login |
| Assemble a book; export **print PDF, screen PDF, Word, EPUB** | all four built in 12 s and download (valid PDF/ZIP) |
| MCP with an access key (header form and the secret `/mcp/k/<key>` form) | 5 tools listed; `list_books`, `search`, `get_passage`, `cite`, `verify_quote` answer; **a revoked key's URL gives 401; the key appears in no log line** |
| Certificate, HSTS, `www` redirect, no `Server` header | pass |
| Backup, then **restore into a scratch database** | identical row counts (books, pages, lines, ledger) |
| **Reboot of the server** | all seven services healthy again 16 s after boot with no action; data, firewall, cron intact |
| Logs of all services after the run | no errors (only a transient Caddy line while `web` restarted during a deploy) |

GPU spend for the test: **61.6 GPU-minutes** (1 h cap, stopped automatically by purging the `gpu` queue; in-flight
pages finished), about **$0.70** at the 24 GB tier price. The remaining **18 pages** (in «شرح الورقات للمحلي»,
«الإهابة…» and the end of «الورقات») stay unread; reading them is «إعادة المعالجة» on those books, about 9 more GPU
minutes.

## 4. Problems found on the first run, and what was done

1. **A Tesseract page took 340 s (up to 615 s).** Three or four Tesseract/Kraken processes at once on 4 vCPUs each start
   one OpenMP thread per core and spin against each other; the pipeline read 2 pages in 8 minutes. Fix:
   `OMP_THREAD_LIMIT=1` and `OMP_NUM_THREADS=1` for every app service (`docker-compose.yml`). A page now takes
   **1.3 s**. This would have made the live demo unusable for anyone uploading a book.
2. **GPU worker memory.** Kraken (about 1.5 GB per reading thread) runs inside the GPU worker's stage, which had a 2 GB
   cap and 4 threads. Now 6 GB and 3 threads.
3. **`deploy.sh` failed on the second deploy** with Docker's containerd image store: building five services into one
   tag errored while cleaning the old image, and tagging the previous image after the build failed. Now it tags the
   previous image first and builds once through `web`. The next deploy after the fix ran end to end.
4. `server-setup.sh` left port 22 allowed in ufw (it read it from the SSH socket); removed by hand. SSH now answers on
   22022 only.
5. Two research tests failed whenever the developer's `.env` had an `MCP_PUBLIC_URL`; the test settings now ignore it.
6. Books 29 and 31 had stale manuscripts (their lines were re-read after the last assembly); re-assembled before the export.

## 5. How to operate it

```sh
ssh -i ~/.ssh/nassakh_vps -p 22022 ubuntu@187.7.65.46
cd /opt/nassakh
docker compose ps                      # everything should say healthy
docker compose logs -f web             # or worker, gpu-worker, mcp, caddy
```

Update: on the Mac `git push vps main` (the key and port are in `docs/DEPLOY.md` §3), then on the server
`bash deploy/deploy.sh`. A deploy takes a database dump first and keeps the previous image (`docs/DEPLOY.md` §11 for the
way back). Never run `docker compose down -v`: it deletes the database and every book.

Give pages to an account: «الفوترة» in the sidebar (superuser). Test confirmation emails: they are in
`docker compose logs web | grep confirm`.

## 6. What is still yours

1. **Email.** Until an SMTP service is set, nobody can confirm a sign-up by email (the link is only in the server log).
   Create an account at Brevo or Resend, add the domain's DNS records they give (SPF, DKIM) in Hostinger, then set
   `EMAIL_BACKEND`, `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `DEFAULT_FROM_EMAIL` in
   `deploy/.env.production` and run `bash deploy/deploy.sh --no-pull`. Outgoing SMTP ports from the Hostinger VPS were
   not tested.
2. **GitHub.** The server's code comes from a private repository on the server. For the challenge: clean `playground/`
   of scanned page images and anything in `.env`-like files, push, make the repository public (the demo scans and the
   bundle are outside the repository by design).
3. **Test accounts and data**: decide whether to keep `tester1`/`tester2` (see §2).
4. **Real connectors.** The MCP works with the secret URL and the Bearer header from a plain client. Connecting Claude
   and ChatGPT from their own settings pages was not tested (it needs your accounts): paste the secret URL from
   «ربط مساعد» in Claude's Connectors (Add custom connector) and in ChatGPT's Developer mode.
5. **Hostinger.** Turn auto-renew off if you want to stop after the month; take a snapshot once the email is set up;
   raise `SECURE_HSTS_SECONDS` (now 3600) when you are sure the domain stays HTTPS-only.
6. **Disk**: the Docker build cache is 10 GB; `docker builder prune -f` now and then.

## 7. Not tested

Sending real email; Claude/ChatGPT web connectors; many people at once (the server was checked with one user and
6 books processing together: web 0.4 GB, worker about 1.2 GB, CPU saturated only by the Tesseract pass before the fix);
a PDF upload near the 500 MB limit; an organisation with several members (no invitation flow exists yet).
