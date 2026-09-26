# App hosting

WebManager normally publishes **static sites**. With app hosting turned on it
can also run **apps**: repositories that run their own server (Node, Python,
Go, …), keep data, and are configured with variables edited in the dashboard.

Each app runs in its own hardened container (Docker) behind WebManager's
Nginx, on a public hostname like any other site.

- [Turning it on](#1-turning-it-on)
- [Security model](#2-security-model-read-this-first)
- [What an app repository must contain](#3-what-an-app-repository-must-contain)
- [The app contract](#4-the-app-contract)
- [`webmanager.json` reference](#5-webmanagerjson-reference)
- [How apps behave in WebManager](#6-how-apps-behave-in-webmanager)
- [Operations: backups, logs, troubleshooting](#7-operations)
- [Example: Uptime-Monitor](#8-example-uptime-monitor)

---

## 1. Turning it on

App hosting is **off by default**. Requirements:

- WebManager installed with `setup.sh` (it uses the managed Nginx).
- At least one deployment domain under **Domains**. Apps always need a public
  hostname; they can't be served on a bare port.
- Internet access from the server while building (to pull base images and
  packages).

On the server, from your WebManager checkout:

```bash
cd ~/webmanager
sudo bash deploy/debian/enable-apps.sh
```

The script explains the risk and asks first. Then it:

1. installs Docker (`docker.io`) if it isn't installed,
2. lets the `webmanager` service use Docker (systemd drop-in
   `/etc/systemd/system/webmanager.service.d/apps.conf` with
   `SupplementaryGroups=docker`),
3. installs `webmanager-app-firewall.service`, which blocks containers from
   reaching the cloud metadata address and every private/link-local IP range
   — apps can reach the internet, not your LAN or the host's other services,
4. sets `WEBMANAGER_APPS_ENABLED=1` in `/etc/webmanager/webmanager.env` and
   restarts WebManager.

Re-running the script (for example after upgrading WebManager) re-applies the
firewall rule set, so it also picks up new blocked ranges on existing
installs.

Check **System** in WebManager: *App hosting* should say **On**. Then allow
people to deploy apps:

- Super admins can always deploy apps.
- Everyone else needs the **Host apps** permission. Easiest: **People &
  access → Teams**, create a team with the **App hosts** profile (or tick
  *Host apps* in custom permissions) and add people to it.
- Limits: **People & access → Default limits → Max apps per person**
  (default **1**; 0 = unlimited). An app also counts toward the person's site
  limit. Per-person overrides are on each person's row.

To turn it off again: `sudo bash deploy/debian/enable-apps.sh --disable`.
Stop or delete apps first; disabling doesn't remove containers or Docker.

### Settings

All optional, in `/etc/webmanager/webmanager.env` (restart WebManager after
changing):

| Variable | Default | Meaning |
|---|---|---|
| `WEBMANAGER_APPS_ENABLED` | `0` | Master switch. |
| `WEBMANAGER_CONTAINER_RUNTIME` | auto | Path or name of the runtime CLI. Auto-detects `docker`, then `podman`. Docker is the tested and supported runtime. |
| `WEBMANAGER_APP_DEFAULT_MEMORY_MB` | `256` | Memory limit for apps whose `webmanager.json` doesn't set `memory_mb`. |
| `WEBMANAGER_APP_CPUS` | `0.5` | CPU limit for every app. |
| `WEBMANAGER_APP_START_TIMEOUT` | `60` | Seconds an app has to pass its health check after starting. |

## 2. Security model (read this first)

**What is isolated.** Every app container runs with:

- a read-only filesystem, except `/data` (the app's volume) and a 64 MB
  `/tmp` (mounted `noexec`),
- all Linux capabilities dropped and `no-new-privileges`,
- memory limit (no extra swap), CPU limit, and at most 256 processes,
- its port published on `127.0.0.1` only: the only way in is through
  WebManager's Nginx on the app's hostname,
- log rotation (3 × 10 MB).

Secrets entered in the dashboard are encrypted in WebManager's database and
passed to the container through a temporary `0600` file, so they never appear
in process lists.

**Network: internet only, by default.** `enable-apps.sh` installs
`webmanager-app-firewall.service`, which blocks every app container from
reaching:

- the cloud metadata address `169.254.169.254` (it can hand out credentials
  for the whole machine on AWS/GCP/Azure/etc.),
- all of `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16` (RFC 1918) and
  `169.254.0.0/16` (link-local) — your LAN, and any service on the host that
  listens on all interfaces (SSH, Nginx on other ports, another database, …).

Outbound internet traffic (webhooks, package installs, HTTP checks) is left
open. If one specific app legitimately needs a LAN address (an internal API,
a mail relay), add a higher-priority `ACCEPT` rule for just that host, above
the `DROP` rules the unit installs:

```bash
sudo iptables -I DOCKER-USER -d 192.168.1.50/32 -j ACCEPT
```

Make it persistent the same way as `webmanager-app-firewall.service`
(a systemd unit, or your distribution's persistent iptables rules file) —
otherwise it's lost on reboot.

**What is not isolated.** Be clear about these before turning it on:

- **Docker access is root-equivalent.** The `webmanager` service account can
  control Docker, so a bug that lets someone run code as WebManager would give
  them the whole server. Run WebManager in a dedicated VM or LXC container if
  that matters to you.
- **Only grant *Host apps* to people you trust** to run code on this machine.
  Static sites never run code; apps do.

## 3. What an app repository must contain

In the repository root, or in the folder you pick when deploying (WebManager
looks up to 4 folders deep, skipping `node_modules`, `vendor`, `.git`, …):

| File | Required | Purpose |
|---|---|---|
| `Dockerfile` | Yes | How to build and run the app. The folder is the build context. |
| `webmanager.json` | Yes | Health check, memory, and the list of settings (section 5). |
| `.dockerignore` | Recommended | Keep `.git`, `data/`, `node_modules/`, `.env` out of the image. |
| `.env.example` | Optional | For humans; WebManager doesn't read it. |

WebManager **refuses to deploy a folder that contains a `.env` file**. Never
commit real secrets; set them in WebManager instead.

## 4. The app contract

An app **must**:

1. **Listen on `0.0.0.0` and the port in `$PORT`** (always `8080` inside the
   container). Don't hard-code another port; a Dockerfile `ENV PORT=…` is
   overridden.
2. **Keep all persistent data in `$DATA_DIR`** (always `/data`). It is the
   only writable place that survives restarts and updates, and it is backed up
   before each restart. `/tmp` is wiped on restart; everything else is
   read-only.
3. **Make `/data` writable by the user the app runs as.** Docker creates the
   volume from the image's `/data` folder, so in the Dockerfile:
   `RUN mkdir -p /data && chown <user> /data` before `USER <user>`.
4. **Use file-based storage** (SQLite, JSON files, …) in `$DATA_DIR`. Separate
   database servers aren't provided.
5. **Answer the health check**: `GET <health path>` must return HTTP `2xx` or `3xx`
   within `WEBMANAGER_APP_START_TIMEOUT` seconds (default 60) of starting.
6. **Read configuration from environment variables** listed in
   `webmanager.json`.
7. **Run as a non-root user** (`USER` in the Dockerfile). Recommended; root in
   the container still has no capabilities, but don't rely on that.
8. **Stop cleanly on `SIGTERM`** within 10 seconds.
9. **Log to stdout/stderr.** The last 200 lines are shown on the app's page.
10. **Work behind a reverse proxy.** Nginx forwards `Host`, `X-Real-IP`,
    `X-Forwarded-For`, `X-Forwarded-Proto` and `X-Forwarded-Host`. Use
    `PUBLIC_URL` for absolute links and OAuth redirect URIs. WebSockets work.

An app **must not** need privileged mode, host networking, extra
capabilities, devices, the Docker socket, more than one port, or to execute
files from `/tmp`.

Limits of the proxy: request bodies up to **25 MB**, and responses must start
within **300 seconds**.

### Variables WebManager always sets

These can't be listed in `webmanager.json` or changed in the dashboard:

| Variable | Value |
|---|---|
| `PORT` | `8080` |
| `HOST` | `0.0.0.0` |
| `DATA_DIR` | `/data` |
| `PUBLIC_URL` | The app's main address, e.g. `https://status.example.com` |
| `TRUST_PROXY` | `true` |
| `WEBMANAGER_APP_ID` | The app's numeric ID in WebManager |

## 5. `webmanager.json` reference

```json
{
  "type": "app",
  "health": "/api/health",
  "memory_mb": 256,
  "env": [
    {
      "name": "ADMIN_PASSWORD",
      "description": "Password for the admin account.",
      "secret": true,
      "required": true
    },
    {
      "name": "SITE_TITLE",
      "description": "Title shown on the status page.",
      "default": "Uptime Monitor"
    },
    {
      "name": "PUBLIC_DASHBOARD",
      "description": "Show the dashboard without signing in.",
      "default": "true",
      "choices": ["true", "false"]
    }
  ]
}
```

| Field | Required | Rules |
|---|---|---|
| `type` | Yes | Must be `"app"`. |
| `health` | Yes | Path starting with `/`, e.g. `"/health"`. |
| `memory_mb` | No | Whole number, 64–4096. Defaults to `WEBMANAGER_APP_DEFAULT_MEMORY_MB` (256). |
| `env` | No | Up to 100 settings shown on the app's **Variables** page. |

Each `env` entry:

| Key | Rules |
|---|---|
| `name` | `UPPER_SNAKE_CASE` (starts with a letter, max 64 chars), unique, not a reserved name above. |
| `description` | Text up to 500 characters, shown under the field. |
| `secret` | `true` = write-only in the dashboard: encrypted, never shown again. |
| `required` | `true` = the app won't start until it has a value (or a `default`). |
| `default` | Single-line string used when the field is empty. |
| `choices` | List of allowed values, shown as a dropdown. `default` must be one of them. |

Values entered in the dashboard must be single-line and at most 8192
characters. Only variables listed in `env` can be set, which keeps typos out
and gives every field a description. The file must be valid JSON under 64 KB.

## 6. How apps behave in WebManager

**Deploying.** Add the repository under **Sources** as usual. On the
*Choose folders* step, any folder with a `Dockerfile` and a valid
`webmanager.json` shows a **Deploy as an app** card (or the reason it can't
be deployed). Pick a name and address and click **Deploy app**. If the app
has required variables, you go to **Variables** first; saving there starts
it.

**Starting and building.** Builds run in the background; the app page shows
*Building and starting…* and refreshes itself. The first build can take
several minutes. Build output is under **Build log** on the app page.

**Variables.** The app page has a **Variables** button. Saving restarts a
running app with the new values. For secrets, leave the field blank to keep
the saved value, or tick **Remove saved value**. Variables are encrypted with
a key derived from WebManager's secret key (`/var/lib/webmanager/secret.key`);
if that file is lost or replaced, re-enter them.

**Restarts and updates.** A restart, a variable change, or a new commit
(approved, or installed automatically depending on the source's update
setting) does this:

1. build a new image if the commit changed,
2. back up `/data`,
3. stop the old container (brief downtime: visitors see *Site temporarily
   unavailable*),
4. start the new one and wait for the health check,
5. if the health check fails, **roll back** to the old container. The app
   keeps running the old version and shows *Needs attention* with the error
   and the new version's logs.

If there is no previous version to roll back to (first start), the app is
marked **Error** instead.

**Stopping.** **Stop** stops the container; visitors get the *Site
temporarily unavailable* page (HTTP 503). The same page is shown whenever the
container isn't answering.

**Settings.** An app's name and addresses can be changed under **Settings**
(it restarts with the new `PUBLIC_URL`). Its folder can't be changed: delete
it and deploy again. The Nginx config editor isn't available for apps.

**Deleting.** **Delete app** removes the container and images. Tick **Also
delete its data** to remove the data volume and backups too; otherwise they
are kept. Removing a whole Git source removes its app containers but keeps
their data.

## 7. Operations

| What | Where |
|---|---|
| Container | `webmanager-app-<id>` |
| Data volume | `webmanager-app-<id>-data` (Docker volume, **not** under `/var/lib/webmanager`) |
| Images | `webmanager-app-<id>:<commit>` (older images are removed after a successful start) |
| Data backups | `/var/lib/webmanager/app-backups/<id>/data-YYYYMMDD-HHMMSS.tar` (last 3 kept) |
| Build log | `/var/lib/webmanager/logs/site-<id>.log` |

**Backing up the server.** The normal WebManager backup (`/var/lib/webmanager`)
includes the automatic app backups above, but not the live volume. For a
current copy:

```bash
sudo docker cp webmanager-app-<id>:/data - > app-<id>-data.tar
```

**Restoring an app's data** from a backup tar:

```bash
sudo docker stop webmanager-app-<id>
sudo docker run --rm -v webmanager-app-<id>-data:/data -v "$PWD":/backup \
    busybox sh -c 'rm -rf /data/* && tar -xf /backup/data-XXXX.tar -C / '
sudo docker start webmanager-app-<id>
```

(`docker cp` archives contain a top-level `data/` folder, so extracting at `/`
restores into `/data`.)

**Troubleshooting.**

| Symptom | Check |
|---|---|
| System says *App hosting: Off* | `WEBMANAGER_APPS_ENABLED=1` in the env file, WebManager restarted, `systemctl status docker`, drop-in present. |
| *The container runtime isn't reachable* | `systemctl show webmanager -p SupplementaryGroups` should list `docker`, and `sudo runuser -u webmanager -g webmanager -G docker -- docker version` should work. If not, re-run `enable-apps.sh`. |
| Build fails | **Build log** on the app page. Base images must be downloadable from the server. |
| *didn't pass its health check* | App logs on the app page. Usually: listening on the wrong port/host, `/data` not writable, or a required variable missing. |
| *Saved app settings could not be decrypted* | WebManager's `secret.key` changed. Re-enter the variables. |
| App restarts repeatedly | **Restarts** count on the app page; often out of memory. Raise `memory_mb`. |

## 8. Example: Uptime-Monitor

[coolguy1333/Uptime-Monitor](https://github.com/coolguy1333/Uptime-Monitor)
already follows the contract (Dockerfile, non-root user, `$PORT`/`$HOST`,
data in `$DATA_DIR`, `/api/health`, graceful `SIGTERM`, `TRUST_PROXY` and
`PUBLIC_URL`). It only needs a `webmanager.json` like the one in section 5,
plus any other settings it supports (for example `GOOGLE_CLIENT_ID`,
`GOOGLE_CLIENT_SECRET` as a secret, `ADMIN_EMAILS`, and
`NOTIFY_WEBHOOK_URL` as a secret).

## Checklist for app authors

- [ ] `Dockerfile` builds with `docker build .` from the app folder
- [ ] Runs as a non-root `USER`; `/data` exists and is owned by that user
- [ ] Listens on `$HOST:$PORT` (port 8080)
- [ ] All persistent data under `$DATA_DIR`
- [ ] Health endpoint returns 200 quickly
- [ ] Handles `SIGTERM`
- [ ] Every setting is an env var listed in `webmanager.json`; secrets marked `"secret": true`
- [ ] No `.env` committed; `.dockerignore` excludes `.git`, `data/`, dependencies
- [ ] Uses `PUBLIC_URL` and honors `X-Forwarded-*`
