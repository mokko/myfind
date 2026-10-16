# Deploying myfind

```
browser ──> nginx :80 ──> gunicorn 127.0.0.1:8010 ──┬─> Solr 127.0.0.1:8983   (search, facets, sort)
                                                    └─> BaseX 0.0.0.0:8080    (record pages, links)
```

Four moving parts, and **as of writing none of them is supervised**: BaseX, Solr
and the app are all orphaned processes. A reboot loses all three, and nothing
brings them back. That is the actual production gap, and these units close it.

## What goes where

| File | Installs to |
|---|---|
| `myfind.service` | `/etc/systemd/system/` |
| `basex.service` | `/etc/systemd/system/` |
| `solr.service` | `/etc/systemd/system/` |
| `nginx-myfind.conf` | `/etc/nginx/sites-available/myfind` |

## Install

```bash
# 1. Dependencies, pinned. gunicorn goes INTO the venv, not via apt -- see below.
cd ~/py/myfind
uv lock
uv pip install -e ".[prod]"

# 2. nginx (the only package that has to come from apt)
sudo apt install -y nginx

# 3. The units
sudo install -m 644 ~/py/myfind/deploy/basex.service  /etc/systemd/system/
sudo install -m 644 ~/py/myfind/deploy/solr.service   /etc/systemd/system/
sudo install -m 644 ~/py/myfind/deploy/myfind.service /etc/systemd/system/

# 4. The proxy config
sudo install -m 644 ~/py/myfind/deploy/nginx-myfind.conf /etc/nginx/sites-available/myfind
sudo ln -sf /etc/nginx/sites-available/myfind /etc/nginx/sites-enabled/myfind
sudo nginx -t                      # validate BEFORE reloading
sudo systemctl reload nginx

# 5. Stop the hand-started processes, then start the supervised ones.
#    Do this in one go so nothing is left running twice and fighting for a port.
sudo systemctl stop  basex solr myfind 2>/dev/null
#    ... stop the orphans first, by port, since systemd did not start them:
for p in 8080 8983 8010; do
  pid=$(ss -ltnp 2>/dev/null | grep ":$p" | grep -oP 'pid=\K[0-9]+' | head -1)
  [ -n "$pid" ] && kill "$pid" && echo "stopped pid $pid on port $p"
done

sudo systemctl daemon-reload
sudo systemctl enable --now basex solr myfind

# 6. Open the port if the firewall needs it
sudo ufw allow 80/tcp
```

## Verify

```bash
systemctl status myfind solr basex --no-pager
curl -s localhost/healthz                      # through nginx
curl -s localhost/Object/447469 | head -5      # a record page, via nginx
journalctl -u myfind -f                        # the app's logs
```

## Traps, each of which cost something to find

**`apt install gunicorn` installs the wrong one.** This box has two Pythons:
system `3.12.3` and the project venv's `3.14.7`. apt's `python3-gunicorn` targets
the system interpreter and **cannot see the venv's packages**, so the app would
fail to import. gunicorn must be installed *into the venv* — `uv pip install -e
".[prod]"` — and the unit calls `.venv/bin/gunicorn` by absolute path.

**Solr must be started with `-f`.** Plain `solr start` daemonises and exits
immediately; under `Type=simple` systemd reads that as "the service died" and
restarts it in a loop. `solr start -f` stays in the foreground.

**`basexhttp` needs no flag** — it ends in `exec java …`, so it is already in the
foreground. Verified by reading the script rather than guessing.

**The proxy timeout must exceed the app timeout.** gunicorn is `--timeout 120`,
nginx is `proxy_read_timeout 130s`. Inverted, nginx returns 504 for exactly the
slowest and most legitimate requests — a cold page query over 127k documents was
measured crossing 30 s.

**Port 8000 is already taken** on this box by the sibling OAI provider
(`uvicorn oai.app:app --port 8000`). myfind listens on **8010**. Check before
assuming a free port:

```bash
for p in 8010 8080 8983; do ss -ltn | grep -q ":$p" && echo "$p busy" || echo "$p free"; done
```

**Nothing restarts a crashed backend today.** `Restart=on-failure` in each unit is
the whole point; a `Restart=always` on BaseX and Solr is defensible too, since a
clean exit there is still a service that is no longer serving.

**BaseX shutdown must not be a SIGKILL.** The unit uses `KillSignal=SIGINT` with
`TimeoutStopSec=90`. A killed JVM mid-write can corrupt a database, and the store
is the authoritative copy.

**`.env` stays mode 600 and is read by the app itself**, so the unit needs no
secret in it. If you prefer a root-owned central file, uncomment
`EnvironmentFile` in `myfind.service`.

## Why gunicorn, and not the development server

Flask's own banner says the development server is not for production, and it
means it: one process, no worker supervision, no request timeout, no graceful
reload, and a single slow request blocks everything behind it. gunicorn adds
process management, timeouts, graceful restarts and access logging.

4 workers is a judgement, not a formula. Every request does **blocking** HTTP to
BaseX or Solr, so concurrency comes from workers rather than threads — a
threaded worker would help only if the app were I/O-async. 4 workers measured
**~45 MB each, 223 MB total**, against 5.2 GB free.

**If this ever becomes more than a proof of concept**, the scaling move is to
convert the app to ASGI (Starlette + `httpx.AsyncClient` + uvicorn/hypercorn),
which is exactly what the sibling OAI project does. The synchronous design was
chosen to suit Flask, and it puts a hard ceiling on concurrency per worker.
