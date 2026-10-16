# Deploying myfind

```
browser ──> nginx :80 ──> uvicorn 127.0.0.1:8010 ──┬─> Solr 127.0.0.1:8983   (search, facets, sort)
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
# 1. Dependencies, pinned. The ASGI server goes INTO the venv, not via apt -- see below.
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

**A server installed from apt is the wrong one.** This box has two Pythons:
system `3.12.3` and the project venv's `3.14.7`. apt's Python packages target the
system interpreter and **cannot see the venv's packages**, so the app would fail
to import. The server must be installed *into the venv* — `uv pip install -e
".[prod]"` — and the unit calls `.venv/bin/uvicorn` by absolute path.

**Solr must be started with `-f`.** Plain `solr start` daemonises and exits
immediately; under `Type=simple` systemd reads that as "the service died" and
restarts it in a loop. `solr start -f` stays in the foreground.

**`basexhttp` needs no flag** — it ends in `exec java …`, so it is already in the
foreground. Verified by reading the script rather than guessing.

**The proxy is now the only request timeout.** Under gunicorn, `--timeout 120`
killed long requests — which is why nginx's `proxy_read_timeout` had to be set
*above* it. Uvicorn has no request timeout at all: `--timeout-keep-alive` governs
idle connections *between* requests, not the length of one. So `proxy_read_timeout
130s` in the nginx config is the effective ceiling, and it must accommodate the
slowest legitimate query — a cold page over 127k documents was measured crossing
30 s.

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

## Why an ASGI server, and not the development server

Flask's banner used to say the development server is not for production, and it
meant it. The equivalent here: `python -m myfind.app` exists for convenience, and
the unit runs the ASGI server directly. That adds worker supervision, timeouts,
graceful restarts, access logging, and — with `--proxy-headers` — correct client
IPs and scheme behind nginx.

**Two workers, not four.** Concurrency no longer comes from process count: one
event loop holds many requests in flight, and the app fans out to the backends
with `asyncio.gather` in three places that the synchronous version could only
serve in series. Workers are now for resilience (one can restart while the other
serves), not for throughput.

Measured: ~55 MB per worker. Note that CPU work — the XML parse on a record page —
is offloaded per request via `anyio.to_thread`, whose default limiter is 40
threads. **That offload is load-bearing.** Without it, a single record with large
CLOBs would block every other request sharing the loop, and the migration would
be a regression rather than a win.

**If this ever needs more headroom**, raise `--workers` first — it is one number,
requires no code change, and 16 workers still cost under a gigabyte. Only if that
proves insufficient is there a design question, and it would be about the
backends, not the framework: Solr and BaseX have their own pools and will
saturate before the event loop does.
