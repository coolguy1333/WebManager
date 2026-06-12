# WebManager for Debian Linux

WebManager is a self-hosted control panel for deploying static websites from Git repositories.

![WebManager logo](webmanager/static/logo.svg)

## Simple setup

Before starting, point both the dashboard domain and its wildcard at the Debian
server:

```text
webmanager.example.com
*.webmanager.example.com
```

Then open a terminal in this project folder and run:

```bash
bash setup.sh
```

The script:

1. Installs Python, Git, Nginx, and WebManager.
2. Starts the required Google sign-in configuration.
3. Can configure HTTPS automatically with Let's Encrypt.
4. Shows the exact callback URL to enter in Google Cloud.
5. Asks for the Google client ID and client secret.

No Google client ID, client secret, callback URL, domain, or email allowlist is
preset in the project. Setup collects these values interactively and stores
them in the protected `/etc/webmanager/webmanager.env` file. Existing complete
Google settings are preserved when setup is run again.

When prompted by the Google setup:

1. Open the displayed Google Cloud link.
2. Configure the OAuth consent screen if Google asks.
3. Create an OAuth client with type **Web application**.
4. Paste the exact **Authorized redirect URI** shown in the terminal.
5. Paste the resulting client ID and secret back into the terminal.

The wildcard DNS record gives deployments addresses such as
`docs.webmanager.example.com`. HTTPS for those addresses requires a wildcard
certificate or an upstream proxy/CDN that covers `*.webmanager.example.com`.

When setup finishes, open:

```text
https://webmanager.example.com
```

Then:

1. Sign in with Google.
2. Paste a Git repository URL.
3. Select one or more folders containing an index page.
4. Click **Deploy selected sites**.

WebManager starts automatically now and after every reboot.

For a non-interactive installation, finish Google setup afterward:

```bash
bash configure-google.sh
```

### Getting the project onto the server

If the project is already on the Debian server, skip this section.

Otherwise, install Git and clone the project:

```bash
sudo apt update
sudo apt install -y git
git clone "https://github.com/coolguy1333/WebManager.git" webmanager
cd webmanager
bash setup.sh
```

### Remove WebManager

Keep all accounts and repository data:

```bash
bash uninstall.sh
```

Delete WebManager and all of its stored data:

```bash
bash uninstall.sh --purge
```

Everything below is optional reference material for custom networking, private repositories, HTTPS, backups, and troubleshooting.

## Reference

- [Features](#features)
- [Requirements](#requirements)
- [Network ports](#network-ports)
- [Quick installation](#quick-installation)
- [Verify the installation](#verify-the-installation)
- [First-time setup](#first-time-setup)
- [Deploy a website](#deploy-a-website)
- [Repository requirements](#repository-requirements)
- [Private Git repositories](#private-git-repositories)
- [Firewall setup](#firewall-setup)
- [Domain name and HTTPS](#domain-name-and-https)
- [Configuration](#configuration)
- [Service management](#service-management)
- [Logs and troubleshooting](#logs-and-troubleshooting)
- [Backup and restore](#backup-and-restore)
- [Upgrade](#upgrade)
- [Uninstall](#uninstall)
- [Development](#development)
- [Security checklist](#security-checklist)

## Features

- Separate user accounts
- Per-user repository and site ownership
- First-account super administrator
- User activation, permission groups, and delegated administration
- Read-only and full-control access across users' deployments
- Public HTTP(S) Git repository support
- SSH Git repository support
- Automatic `index.html` and `index.htm` discovery
- Selectable document root within a repository
- Automatic collision-free internal site ports
- Per-site public subdomains on one or more domains
- Generated Nginx server configurations
- Browser-based Nginx configuration editor
- Static single-page application fallback
- Multi-site deployment from one repository inspection
- Editable site name, hostname slug, source folder, port, and fallback settings
- Per-site traffic analytics from structured Nginx logs
- Proxmox-style pools with user and group ACLs
- Immediate start, stop, restart, update, and delete controls
- Validated site updates with owner approval or automatic application
- Per-repository Git update-check intervals
- GitHub update checks with super-admin-approved installation
- Debian systemd service
- Waitress application server
- Nginx reverse proxy for the dashboard
- Unprivileged Nginx process for deployed sites
- Persistent SQLite database and repository storage
- Health endpoint for monitoring
- Google OpenID Connect sign-in
- Optional Google Workspace domain and email allowlists

## Requirements

### Supported operating system

- Debian 12 or newer
- A normal Debian installation using systemd
- `root` access or a user with `sudo`
- A domain name for production Google sign-in

The automatic installer is intended for a Debian host or virtual machine. It is not designed for shared hosting without root access. Google permits insecure HTTP callbacks only for localhost development, so a production server should use a domain and HTTPS.

### Network access

The server needs outbound access to:

- Debian package repositories
- Python package indexes
- Any Git hosts used by deployments

### Hardware

WebManager itself is small. A basic server is normally enough:

- 1 CPU core
- 512 MB RAM minimum
- 1 GB RAM recommended
- Disk space for cloned repositories and backups

Large repositories may require more memory, storage, and clone time.

## Network ports

The default installation uses:

| Port | Bind address | Purpose |
| --- | --- | --- |
| `5000/tcp` | `127.0.0.1` | Internal Waitress dashboard server |
| `8080/tcp` | All interfaces | Public Nginx dashboard endpoint |
| `8090/tcp` | `127.0.0.1` | Internal hostname-routing gateway |
| `8100-8999/tcp` | `127.0.0.1` | Per-site internal endpoints |

Port `5000` should not be exposed publicly. Debian's system Nginx forwards dashboard traffic from port `8080` to `127.0.0.1:5000`.

Each deployed site receives one internal port from `8100` through `8999`.
System Nginx accepts wildcard HTTP traffic on port `80` and forwards it to the
loopback-only gateway on port `8090`. HTTPS can terminate at an upstream
proxy/CDN, or at system Nginx after a wildcard certificate is installed.
Dashboard HTTPS does not automatically cover deployed site names. The setup
helper asks separately whether wildcard TLS covers
`*.webmanager.example.com`; until it does, generated Open links use HTTP.

Cloudflare Tunnel can carry the dashboard and every hosted site through one
local service. Route both the dashboard hostname and the wildcard hostname to
the same tunnel service:

```text
webmanager.example.com   -> http://localhost:8080
*.example.com            -> http://localhost:8080
```

Nginx routes requests on port `8080` by their original hostname.
The exact dashboard entry does not match subdomains. Both Cloudflare public
hostnames are required. For a dashboard at `web.mhsit.club`, use
`*.mhsit.club` for hosted sites such as `demo.mhsit.club`.

Cloudflare's standard zone certificate commonly covers the zone apex and
first-level wildcard only. Keeping site names directly under the zone, such as
`demo.mhsit.club`, allows that normal wildcard coverage to apply.

When the zone-level wildcard `*.mhsit.club` is used, the dashboard Nginx
server must have the exact `server_name web.mhsit.club`. Setup derives that
name from the Google callback URL so the exact dashboard route takes priority
over the wildcard site route.

Super admins can add more interface hostnames under **Admin > Domains >
Dashboard domains**. One is Primary and the others are aliases. For each
hostname, add an exact Cloudflare Tunnel route to
`http://localhost:8080` and add this URI to the existing Google OAuth web
client:

```text
https://HOSTNAME/auth/google/callback
```

Google sign-in returns to the same approved dashboard hostname that initiated
the login. Dashboard hostnames are reserved and cannot also host deployed
sites.

The internal site ports should not be opened in UFW or a cloud firewall.

Before installation, check for port conflicts:

```bash
sudo ss -ltnp
```

If another service already uses port `8080`, either stop that service or edit `deploy/debian/nginx-dashboard.conf` before installation.

## Quick installation

From the downloaded project folder:

```bash
bash setup.sh
```

The script performs the entire installation and prints the dashboard URL. Run the same command again to upgrade.

When the project was cloned from GitHub, setup automatically uses its HTTPS
`origin` and current branch for program updates. A downloaded archive uses the
official WebManager repository and `main` branch automatically.

For unattended setup or to configure the source explicitly:

```bash
bash setup.sh \
  --update-repository https://github.com/coolguy1333/WebManager.git \
  --update-branch main
```

The source folder must not be `/opt/webmanager`; that path is managed by the installer.

## Verify the installation

Check the WebManager service:

```bash
sudo systemctl status webmanager --no-pager
```

Check the system Nginx service:

```bash
sudo systemctl status nginx --no-pager
```

Test the local application health endpoint:

```bash
curl http://127.0.0.1:5000/healthz
```

Expected response:

```json
{"status":"ok"}
```

Before HTTPS configuration, test the dashboard through Nginx:

```bash
curl -I http://127.0.0.1:8080/
```

After Google and HTTPS configuration, test the public address:

```bash
curl -I https://webmanager.example.com/
```

Open:

```text
https://webmanager.example.com
```

If the page does not open, review [Firewall setup](#firewall-setup) and [Logs and troubleshooting](#logs-and-troubleshooting).

## First-time setup

### 1. Configure Google sign-in

If setup did not already configure Google:

```bash
bash configure-google.sh
```

For production, the helper can configure HTTPS with Let's Encrypt. It then pauses while you create a Google OAuth client and shows the exact callback URL to use.

If the Google OAuth consent screen is in **Testing** mode, add each permitted Google account as a test user in Google Cloud. Otherwise, Google may reject the sign-in before returning to WebManager.

### 2. Sign in

Open the HTTPS address configured during setup and select **Continue with Google**.

The first successful sign-in automatically creates the WebManager account. WebManager stores Google's stable account identifier, verified email, display name, and optional profile picture. It never receives or stores the Google password.

Each Google account gets separate repositories and deployments.

### Administration, groups, and permissions

The first Google account that signs in becomes the initial **super
administrator**. On an upgraded installation, the oldest existing account is
promoted automatically if no administrator exists.

Administrators have an **Admin** link in the navigation. The console separates
tasks into **Overview**, **People**, **Groups**, **Access**, and **Updates** so
only the controls for the selected task are displayed. Account, group, pool,
and site editors stay collapsed until opened.

The admin console can:

- Enable or disable user accounts
- Promote additional super administrators
- Create and delete permission groups
- Assign users to one or more groups
- Choose the permissions granted by each group
- Create Proxmox-style resource pools for sites
- Grant Viewer or Operator access to a pool or an individual site
- Add multiple deployment domains and choose the default for new sites

Available permissions:

| Permission | Access |
| --- | --- |
| `View all resources` | Read-only access to every user's sites and repositories |
| `Manage all resources` | Start, stop, edit, refresh, schedule, deploy, and delete across users |
| `Manage users` | Activate accounts and assign group memberships |
| `Manage groups` | Create and edit permission groups |
| `Manage pools and access` | Create pools and assign site ACLs to users or groups |

Each site can belong to one pool. Pool access and direct site access use two
roles:

| Site role | Access |
| --- | --- |
| `Viewer` | See the site in the dashboard, inspect its details, and open it |
| `Operator` | Viewer access plus start, stop, restart, edit Nginx configuration, and delete |

Use **Admin > Access > Pools** to assign normal site access. Use **Direct site
access** only for exceptions. A direct site
grant is useful for an exception without exposing the whole pool. When several
grants apply, the strongest role wins. Site ACLs do not grant repository
control, so source updates and deployment settings remain with the repository
owner or a user with `Manage all resources`.

Super administrators bypass all permission checks. Delegated administrators
cannot grant permissions they do not already possess. WebManager also prevents
an administrator from disabling their own account or removing the final active
super administrator.

Disabled users are signed out on their next request and cannot complete Google
sign-in until an administrator reactivates them.

### Admin-managed deployment domains

Super administrators can open **Admin > Domains** to add more base domains.
Existing sites keep their assigned domain. New deployments use the default
domain unless the owner selects another configured domain, and a site's domain
can be changed later from its settings page.

A dashboard hostname may also be registered as a deployment domain. WebManager
reserves only the exact dashboard hostname: subdomain deployments remain
available, while root hosting at that exact address is blocked.

A site may also use additional domain aliases. Choose one primary domain and
check any additional domains during deployment or from **Site settings**. The
same slug and address style apply to every selected domain, so `docs` can serve
both `docs.example.com` and `docs.example.net`. The site detail page lists every
public URL, and analytics combine traffic from all of them.

A site can also be assigned to the domain root, such as `https://mhsit.club`,
instead of `https://site.mhsit.club`. Only one site can claim each domain root,
and the WebManager dashboard hostname cannot be assigned to a site. Root mode
can use multiple domains, such as both `example.com` and `example.net`, provided
no other site owns either root.

The deployment screen presents **Site subdomain** and **Domain root** as
separate address choices and previews the resulting URL. Root mode
automatically limits the deployment to one selected folder and reports when
another site already owns that domain root.

Interactive `setup.sh` requires the first deployment domain and creates it as
the default. Reinstalling or updating preserves that domain. Setup never
guesses the deployment domain from the dashboard hostname.

The same page includes a deployment-domain blocklist. Entries may be separated
by commas or new lines. Blocking `example.com` also blocks every subdomain,
including `school.example.com`. Blocked domains cannot be added, selected for
new deployments, assigned to another site, or made the default. Existing sites
using a newly blocked domain remain online and are marked so an administrator
can move them without an unexpected outage.

For every primary or additional domain used by a site, the Domains page and
site detail page display the required Cloudflare configuration:

1. For root hosting, add the exact published hostname `example.com` with no
   subdomain or path.
2. For site subdomains, separately add `*.example.com`.
3. Send both entries to `http://localhost:8080`, or to
   `http://WEBMANAGER-LAN-IP:8080` when `cloudflared` runs elsewhere.
   Keep `:8080` on both routes. Omitting it uses port 80, where Debian's
   default Nginx site may intercept the request.
4. Confirm Cloudflare DNS has proxied Tunnel/CNAME records for the zone apex
   and wildcard `*`.
5. Verify root routing with
   `curl -I -H 'Host: example.com' http://127.0.0.1:8080/`.
6. Verify subdomain DNS with `nslookup test.example.com 1.1.1.1`.

System Nginx accepts candidate hostnames on port `8080`, but the internal
loopback gateway serves only exact site hostnames stored by WebManager.
Unconfigured hostnames return `404`.

### 3. Restrict who may sign in

If both allowlists are left blank, any Google account with a verified email can
create an active WebManager account. Setup requires explicit confirmation
before enabling this unrestricted mode, and the admin page displays a warning
while it remains enabled.

To restrict access, run:

```bash
bash configure-google.sh
```

Enter one or both of:

- Google Workspace domains, such as `example.com`
- Individual Google account emails

When both lists are present, an account is accepted when its verified email is listed or Google's `hd` claim matches an allowed Workspace domain.

### Link an account from an older WebManager version

If this installation already has username/password users, link each old username to its Google email before that user signs in:

```bash
sudo -u webmanager env \
  WEBMANAGER_DATA_DIR=/var/lib/webmanager \
  /opt/webmanager/.venv/bin/flask \
  --app /opt/webmanager/run.py \
  link-google-user \
  --username OLD_USERNAME \
  --email PERSON@example.com
```

At the next matching Google sign-in, the old user record is upgraded in place. Its repositories, deployments, ports, and configurations remain attached to the same user ID.

## Deploy a website

### 1. Prepare the repository

The repository must contain a ready-to-serve static website with one of:

```text
index.html
index.htm
```

The index file may be in the repository root or in a subfolder such as:

```text
public/
dist/
build/
docs/
site/
```

### 2. Inspect the repository

From the dashboard:

1. Enter the Git repository URL.
2. Optionally enter a branch name.
3. Select **Clone and inspect**.

Supported URL examples:

```text
https://github.com/example/project.git
ssh://git@github.com/example/project.git
git@github.com:example/project.git
```

Local paths and `file://` URLs are rejected.

### 3. Select the document root

WebManager searches the cloned repository for `index.html` and `index.htm`.

Choose the folder that should become the site's document root. Dependency and metadata directories such as `.git`, `node_modules`, `.venv`, and `__pycache__` are ignored.

### 4. Configure the deployment

Enter:

- A site name
- An optional internal port
- Whether single-page application fallback should be enabled

If no port is entered, WebManager selects the first available port in the configured range.

With SPA fallback enabled, routes such as `/account/settings` load the selected index page when no matching file exists.

### 5. Open the site

After deployment:

```text
https://SITE-SLUG.webmanager.example.com
```

All public domain addresses and the internal assigned port are shown on the
site detail page.

### Site source updates

Each saved repository has a **Site updates** control on the dashboard.

Enter an interval in minutes and select **Save**. WebManager accepts intervals
from `5` minutes through `43200` minutes (30 days). Leave the field blank and
save to disable scheduled checks while keeping the manual **Check update**
button available.

Choose one update mode:

- **Owner approval** stages a validated revision without changing live files.
  The repository owner reviews the affected sites and selects **Approve** or
  **Discard**.
- **Apply automatically** activates a validated revision immediately after a
  manual or scheduled check.

The schedule is stored in SQLite and resumes after WebManager or Debian
restarts. The dashboard shows the next scheduled run in UTC and the date of the
last successful pull.

Every check uses a fresh shallow clone in a separate pending directory. A
failed clone or an update waiting for approval leaves the currently hosted
files unchanged. WebManager rejects an update if it would remove a folder or
index file used by an existing deployment. Approved and automatic updates use
an atomic directory swap, so every site sharing that repository moves to the
same validated revision together.

Stopping a site removes its hostname route and immediately restarts only the
internal managed Nginx gateway. This prevents a graceful-reload worker from
continuing to serve the stopped site through an existing keep-alive connection.

## Repository requirements

WebManager hosts static files. It does not currently run application build commands.

### Supported directly

- Plain HTML, CSS, and JavaScript
- Prebuilt React, Vue, Svelte, Angular, or other frontend output
- Static documentation
- Static assets and single-page applications

### Not built automatically

WebManager does not automatically run:

```text
npm install
npm run build
yarn build
pnpm build
vite build
gatsby build
hugo
jekyll
```

For a frontend framework, commit or publish its generated output folder to the Git repository, then select that folder during deployment.

For example, a Vite project normally needs a committed or generated `dist/index.html`. WebManager should deploy `dist`, not the unbuilt source directory.

### Not supported as application runtimes

This version does not launch:

- Node.js servers
- Python web applications
- PHP applications
- Databases
- Docker Compose projects
- Server-side rendered applications

It is specifically a static website manager.

## Private Git repositories

SSH deploy keys are the recommended way to access private repositories.

WebManager runs Git as the restricted `webmanager` Linux account with:

```text
HOME=/var/lib/webmanager
```

Interactive password prompts are disabled.

### Create an SSH key

Create the SSH directory:

```bash
sudo install -d \
  -o webmanager \
  -g webmanager \
  -m 0700 \
  /var/lib/webmanager/.ssh
```

Generate an Ed25519 key:

```bash
sudo -u webmanager \
  ssh-keygen \
  -t ed25519 \
  -f /var/lib/webmanager/.ssh/id_ed25519 \
  -N ""
```

Display the public key:

```bash
sudo cat /var/lib/webmanager/.ssh/id_ed25519.pub
```

Add that public key to the Git provider as a read-only deploy key.

### Add the Git host key

For GitHub:

```bash
sudo -u webmanager \
  ssh-keyscan github.com \
  | sudo tee /var/lib/webmanager/.ssh/known_hosts >/dev/null
```

For GitLab:

```bash
sudo -u webmanager \
  ssh-keyscan gitlab.com \
  | sudo tee /var/lib/webmanager/.ssh/known_hosts >/dev/null
```

Set ownership and permissions:

```bash
sudo chown webmanager:webmanager /var/lib/webmanager/.ssh/known_hosts
sudo chmod 0600 /var/lib/webmanager/.ssh/known_hosts
```

Verify the host fingerprint through a trusted source before accepting it on an Internet-facing production server.

### Test repository access

Test without cloning:

```bash
sudo -u webmanager \
  env HOME=/var/lib/webmanager GIT_TERMINAL_PROMPT=0 \
  git ls-remote git@github.com:OWNER/REPOSITORY.git
```

Replace the sample URL with the private repository URL.

If refs are returned, WebManager should be able to clone the repository.

## Firewall setup

The required public ports are:

```text
80/tcp
443/tcp
```

Port `8080` is used only by the initial HTTP dashboard before automatic HTTPS
configuration. Do not expose ports `5000`, `8090`, or `8100-8999`; they are
loopback-only application and site endpoints.

### UFW

The installer adds rules automatically only when UFW is installed and already active.

Check UFW:

```bash
sudo ufw status verbose
```

Add the rules manually when needed:

```bash
sudo ufw allow 8080/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw allow 8100:8999/tcp
sudo ufw reload
```

To restrict the dashboard to one administrator IP:

```bash
sudo ufw delete allow 8080/tcp
sudo ufw allow from ADMIN_IP to any port 8080 proto tcp
```

Replace `ADMIN_IP` with the public IP that should manage WebManager.

### Cloud firewall or security group

Virtual servers from cloud providers may have a firewall outside Debian. Allow:

- TCP `8080` for the dashboard
- TCP `80` and `443` for the HTTPS dashboard
- TCP `8100-8999` for deployed sites

Restrict dashboard port `8080` to trusted IP addresses when possible.

### Verify listening ports

```bash
sudo ss -ltnp | grep -E ':(5000|8080|81[0-9][0-9]|8[2-9][0-9][0-9])\b'
```

## Domain name and HTTPS

The default setup uses HTTP on port `8080`. HTTPS is strongly recommended before exposing the dashboard to the Internet.

The instructions below secure the dashboard. They do not automatically add domains or TLS certificates to sites deployed on ports `8100-8999`.

The easiest option is:

```bash
bash configure-google.sh
```

Answer `Y` when it offers to configure HTTPS with Let's Encrypt. The manual steps below are for custom setups.

### 1. Configure DNS

Create an `A` record:

```text
webmanager.example.com -> SERVER_IPV4_ADDRESS
*.webmanager.example.com -> SERVER_IPV4_ADDRESS
```

Create an `AAAA` record too if the server uses public IPv6.

Wait for DNS to resolve:

```bash
getent hosts webmanager.example.com
getent hosts test.webmanager.example.com
```

The wildcard record is required for deployed site subdomains.

### 2. Update the dashboard Nginx server

Edit:

```bash
sudo nano /etc/nginx/sites-available/webmanager
```

Change the listening and server-name lines to:

```nginx
listen 80;
listen [::]:80;
server_name webmanager.example.com;
```

Leave the existing `location /` proxy block in place.

Validate and reload:

```bash
sudo nginx -t
sudo systemctl reload nginx
```

Allow HTTP and HTTPS through the firewall:

```bash
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
```

### 3. Install Certbot

```bash
sudo apt update
sudo apt install -y certbot python3-certbot-nginx
```

Request the certificate:

```bash
sudo certbot --nginx -d webmanager.example.com
```

Follow Certbot's prompts and choose HTTP-to-HTTPS redirection.

Test certificate renewal:

```bash
sudo certbot renew --dry-run
```

### 4. Enable secure session cookies

Edit:

```bash
sudo nano /etc/webmanager/webmanager.env
```

Set:

```text
WEBMANAGER_SESSION_COOKIE_SECURE=1
```

Restart:

```bash
sudo systemctl restart webmanager
```

Open:

```text
https://webmanager.example.com
```

### Protect the HTTPS configuration

The Debian installer preserves an existing dashboard Nginx file at:

```text
/etc/nginx/sites-available/webmanager
```

It is still a good idea to back up domain and Certbot changes before an upgrade:

```bash
sudo cp \
  /etc/nginx/sites-available/webmanager \
  /root/webmanager-nginx-backup.conf
```

After an upgrade, validate the configuration:

```bash
sudo nginx -t
sudo systemctl reload nginx
```

## Configuration

The production environment file is:

```text
/etc/webmanager/webmanager.env
```

Default contents:

```text
WEBMANAGER_DATA_DIR=/var/lib/webmanager
WEBMANAGER_HOST=127.0.0.1
WEBMANAGER_PORT=5000
WEBMANAGER_SITE_PORT_MIN=8100
WEBMANAGER_SITE_PORT_MAX=8999
WEBMANAGER_SITE_GATEWAY_PORT=8090
WEBMANAGER_SITE_BASE_DOMAIN=
WEBMANAGER_SITE_PUBLIC_SCHEME=http
WEBMANAGER_NGINX_BINARY=/usr/sbin/nginx
WEBMANAGER_TRUST_PROXY=1
WEBMANAGER_SESSION_COOKIE_SECURE=0
WEBMANAGER_GOOGLE_CLIENT_ID=
WEBMANAGER_GOOGLE_CLIENT_SECRET=
WEBMANAGER_GOOGLE_REDIRECT_URI=
WEBMANAGER_GOOGLE_ALLOWED_DOMAINS=
WEBMANAGER_GOOGLE_ALLOWED_EMAILS=
WEBMANAGER_AUTO_REFRESH_ENABLED=1
WEBMANAGER_AUTO_REFRESH_POLL_SECONDS=30
WEBMANAGER_DEBUG=0
PYTHONUNBUFFERED=1
PYTHONDONTWRITEBYTECODE=1
GIT_TERMINAL_PROMPT=0
```

### Application settings

| Variable | Default | Purpose |
| --- | --- | --- |
| `WEBMANAGER_DATA_DIR` | `/var/lib/webmanager` | Database, repositories, logs, secret, and managed Nginx state; keep this default in production |
| `WEBMANAGER_HOST` | `127.0.0.1` | Internal dashboard bind address; keep loopback-only behind Nginx |
| `WEBMANAGER_PORT` | `5000` | Internal Waitress dashboard port |
| `WEBMANAGER_SITE_PORT_MIN` | `8100` | First assignable site port |
| `WEBMANAGER_SITE_PORT_MAX` | `8999` | Last assignable site port |
| `WEBMANAGER_SITE_GATEWAY_PORT` | `8090` | Loopback gateway used by wildcard system Nginx |
| `WEBMANAGER_SITE_BASE_DOMAIN` | empty | Base domain appended to each globally unique site slug |
| `WEBMANAGER_SITE_PUBLIC_SCHEME` | `http` | Public site URL scheme, normally `https` |
| `WEBMANAGER_NGINX_BINARY` | `/usr/sbin/nginx` | Nginx executable used for deployed sites |
| `WEBMANAGER_TRUST_PROXY` | `1` | Trust one local reverse-proxy hop |
| `WEBMANAGER_SESSION_COOKIE_SECURE` | `0` | Send session cookies only over HTTPS when set to `1` |
| `WEBMANAGER_GOOGLE_CLIENT_ID` | empty | Google OAuth web client ID |
| `WEBMANAGER_GOOGLE_CLIENT_SECRET` | empty | Google OAuth client secret |
| `WEBMANAGER_GOOGLE_REDIRECT_URI` | empty | Exact authorized callback URL |
| `WEBMANAGER_GOOGLE_ALLOWED_DOMAINS` | empty | Comma-separated Workspace `hd` claims |
| `WEBMANAGER_GOOGLE_ALLOWED_EMAILS` | empty | Comma-separated verified Google emails |
| `WEBMANAGER_AUTO_REFRESH_ENABLED` | `1` | Enables the background repository scheduler |
| `WEBMANAGER_AUTO_REFRESH_POLL_SECONDS` | `30` | How often the scheduler checks for due repositories |
| `WEBMANAGER_DEBUG` | `0` | Flask debugging; keep disabled in production |

After editing the file:

```bash
sudo systemctl restart webmanager
```

The hardened systemd unit grants write access to `/var/lib/webmanager`. Changing `WEBMANAGER_DATA_DIR` also requires a matching systemd `ReadWritePaths` override. Keeping the Debian default is recommended.

### Changing the dashboard internal port

If `WEBMANAGER_PORT` changes, update `proxy_pass` in:

```text
/etc/nginx/sites-available/webmanager
```

For example:

```nginx
proxy_pass http://127.0.0.1:NEW_PORT;
```

Then run:

```bash
sudo nginx -t
sudo systemctl reload nginx
sudo systemctl restart webmanager
```

Keeping the default internal port is recommended.

### Changing the deployed-site port range

Change both:

```text
WEBMANAGER_SITE_PORT_MIN
WEBMANAGER_SITE_PORT_MAX
```

These ports remain loopback-only and should not be opened in UFW, the cloud
firewall, or network security groups. Existing deployments keep their assigned
database ports. Choose a new range that still includes existing ports or
recreate those deployments.

## Service management

Start WebManager:

```bash
sudo systemctl start webmanager
```

Stop WebManager and its child site processes:

```bash
sudo systemctl stop webmanager
```

Restart:

```bash
sudo systemctl restart webmanager
```

Enable startup at boot:

```bash
sudo systemctl enable webmanager
```

Disable startup at boot:

```bash
sudo systemctl disable webmanager
```

Display status:

```bash
sudo systemctl status webmanager --no-pager
```

Reload Debian's system Nginx:

```bash
sudo nginx -t
sudo systemctl reload nginx
```

## Installed files and directories

```text
/opt/webmanager/
  run.py
  requirements.txt
  README.md
  webmanager/
  .venv/

/var/lib/webmanager/
  webmanager.sqlite3
  secret.key
  repositories/
  nginx/
    nginx.conf
    nginx.pid
    conf.d/
    temp/
    access.log
    error.log
  logs/
  .ssh/

/etc/webmanager/
  webmanager.env
  updater.env

/etc/systemd/system/
  webmanager.service
  webmanager-update.service
  webmanager-update.timer
  webmanager-update.path

/usr/local/sbin/
  webmanager-update

/var/lib/webmanager-updater/
  status.json
  requests/
  backups/
  Temporary check directories

/etc/nginx/sites-available/
  webmanager
  webmanager-sites

/etc/nginx/sites-enabled/
  webmanager
  webmanager-sites
```

Ownership:

- Application code: `root:root`
- Runtime data: `webmanager:webmanager`
- Environment file: `root:webmanager`

Do not manually run WebManager as root.

## Logs and troubleshooting

### Dashboard does not load

Check the service:

```bash
sudo systemctl status webmanager --no-pager
sudo journalctl -u webmanager -n 100 --no-pager
```

Check the health endpoint:

```bash
curl -v http://127.0.0.1:5000/healthz
```

If the health endpoint works but port `8080` does not, inspect system Nginx:

```bash
sudo nginx -t
sudo systemctl status nginx --no-pager
sudo tail -n 100 /var/log/nginx/error.log
```

### Nginx returns `502 Bad Gateway`

This normally means system Nginx cannot reach Waitress.

Run:

```bash
sudo systemctl restart webmanager
sudo journalctl -u webmanager -n 100 --no-pager
sudo ss -ltnp | grep ':5000'
```

Confirm `proxy_pass` uses the same host and port as `/etc/webmanager/webmanager.env`.

### Repository clone fails

View service logs:

```bash
sudo journalctl -u webmanager -n 100 --no-pager
```

Test repository access as the service account:

```bash
sudo -u webmanager \
  env HOME=/var/lib/webmanager GIT_TERMINAL_PROMPT=0 \
  git ls-remote REPOSITORY_URL
```

Common causes:

- Incorrect repository URL
- Missing deploy key
- Deploy key not added to the repository
- Missing SSH host key
- Branch does not exist
- Server has no outbound Internet access

### No index page is found

Confirm the repository contains:

```text
index.html
```

Or:

```text
index.htm
```

If the project requires a build, generate and commit the output directory first.

### A deployed site does not open

Check its assigned port in the dashboard, then run:

```bash
sudo ss -ltnp | grep ':ASSIGNED_PORT'
```

Check the managed Nginx error log:

```bash
sudo tail -n 100 /var/lib/webmanager/nginx/error.log
```

Validate the managed Nginx configuration:

```bash
sudo -u webmanager \
  /usr/sbin/nginx \
  -t \
  -p /var/lib/webmanager/nginx/ \
  -c nginx.conf
```

Check firewall rules:

```bash
sudo ufw status verbose
```

Also check the cloud provider's firewall or security group.

### Port already in use

Identify the process:

```bash
sudo ss -ltnp | grep ':PORT'
```

Choose another site port or change the configured range.

### Permission denied

Inspect path permissions:

```bash
sudo namei -l /var/lib/webmanager
sudo find /var/lib/webmanager -maxdepth 2 -printf '%M %u:%g %p\n'
```

Restore expected ownership:

```bash
sudo chown -R webmanager:webmanager /var/lib/webmanager
sudo chmod 0750 /var/lib/webmanager
sudo systemctl restart webmanager
```

### View logs continuously

Dashboard and service:

```bash
sudo journalctl -u webmanager -f
```

System Nginx:

```bash
sudo tail -f /var/log/nginx/access.log /var/log/nginx/error.log
```

Managed site Nginx:

```bash
sudo tail -f \
  /var/lib/webmanager/nginx/access.log \
  /var/lib/webmanager/nginx/error.log
```

Fallback static-server logs, if Nginx is unavailable:

```text
/var/lib/webmanager/logs/site-SITE_ID.log
```

## Backup and restore

The data directory contains:

- Google account identifiers and profile details
- Repository records
- Cloned repositories
- Site records and assigned ports
- Edited Nginx configurations
- Application secret
- Runtime logs

Store backups securely.

### Create a backup

Stop WebManager for a consistent SQLite and repository snapshot:

```bash
sudo systemctl stop webmanager
sudo tar \
  -C /var/lib \
  -czf /root/webmanager-backup-$(date +%F).tar.gz \
  webmanager
sudo systemctl start webmanager
```

Verify the archive:

```bash
sudo tar -tzf /root/webmanager-backup-$(date +%F).tar.gz | head
```

### Restore a backup

Install WebManager first if it is not installed, then stop it:

```bash
sudo systemctl stop webmanager
```

Move the current data directory out of the way:

```bash
sudo mv /var/lib/webmanager /var/lib/webmanager.before-restore
```

Extract the backup:

```bash
sudo tar -C /var/lib -xzf /root/webmanager-backup-YYYY-MM-DD.tar.gz
```

Restore ownership:

```bash
sudo chown -R webmanager:webmanager /var/lib/webmanager
sudo chmod 0750 /var/lib/webmanager
```

Start and verify:

```bash
sudo systemctl start webmanager
curl http://127.0.0.1:5000/healthz
```

Keep `/var/lib/webmanager.before-restore` until the restored installation is confirmed.

## Upgrade

### Super-admin-approved application updates

The Debian installer creates a systemd timer that checks the configured GitHub
branch every 15 minutes. Checks never install code. When a newer commit is
available, a super administrator must open **Admin**, review the exact commit,
and select **Approve and install**.

Setup runs the first check immediately. A super administrator can also select
**Check now** in the Admin page; this asks the hardened systemd updater service
to run without granting the web process root access.

When application requirements are unchanged, update validation reuses the
installed virtual environment first. If that test run fails, the updater
automatically retries in a clean virtual environment before rejecting the
update. Failures are reported separately for environment creation, dependency
installation, and application tests, with the final test output shown in the
admin update status.

If installation fails after WebManager is stopped, rollback restores the
application, configuration, and persistent data, restarts the service, and
waits for `/healthz` before completing. A failed recovery is recorded in the
update status and updater journal.

Rollback restores the contents of systemd-protected writable directories in
place. It never attempts to remove `/opt/webmanager`, `/var/lib/webmanager`, or
`/etc/webmanager` themselves because systemd may expose those paths as service
mount points.

The updater also performs a second independent `/healthz` check after the
installer returns. Its exit handler attempts an emergency service restart if
the updater is interrupted or exits unexpectedly while WebManager is offline.

Running `bash setup.sh` over an existing installation also reuses a healthy
virtual environment when requirements are unchanged. The database, repository
clones, site logs, and Google configuration remain under `/var/lib/webmanager`
and `/etc/webmanager` and are not replaced. If the installed virtual
environment is missing or unhealthy, setup builds and validates a replacement
beside it, then atomically swaps only `/opt/webmanager/.venv`. A failed package
installation leaves the active environment untouched. The previous environment
is retained until the restarted service passes `/healthz` and is restored
automatically if any later installation step fails.

Replacement virtual environments are installed with traverse permissions for
the unprivileged `webmanager` service account while remaining owned by root.

On an existing installation, manual setup preserves an intentionally disabled
updater timer/path and clears stale update request files after the application
passes its health check. New installations still enable update checks by
default when they are configured. A successful manual setup also replaces any
stale testing or installing status with the exact commit it installed.

An updater-driven self-update never enables, disables, or starts its own timer
and path units while the updater service is active. This prevents the approval
file from recursively retriggering installation.

This updates WebManager itself and is separate from owner-approved or automatic
site source updates in the dashboard.

Before installing a new commit, the updater:

1. Clones the configured branch into an isolated temporary directory.
2. Verifies that the URL is an HTTPS `github.com` repository.
3. Rejects force-pushed or rewritten history.
4. Requires approval for that exact 40-character commit from a super admin.
5. Creates a clean virtual environment and runs the full test suite.
6. Stops WebManager and backs up `/var/lib/webmanager`,
   `/etc/webmanager`, the installed application, and service definitions.
7. Installs the candidate and waits for the health endpoint.
8. Automatically restores both the previous application and all persistent
   data if installation or health verification fails.

The database, repositories, secrets, Google configuration, Nginx dashboard
configuration, and deployed-site data remain in `/var/lib/webmanager` and
`/etc/webmanager`. Successful updates preserve them. The three most recent
pre-update backups are retained under `/var/lib/webmanager-updater/backups`.

Check update status:

```bash
sudo systemctl status webmanager-update.timer --no-pager
sudo systemctl list-timers webmanager-update.timer
```

Run a check immediately:

```bash
sudo systemctl start webmanager-update.service
sudo journalctl -u webmanager-update.service -n 100 --no-pager
```

Configuration is stored in:

```text
/etc/webmanager/updater.env
```

Example:

```text
WEBMANAGER_UPDATE_ENABLED=1
WEBMANAGER_UPDATE_REPOSITORY=https://github.com/coolguy1333/WebManager.git
WEBMANAGER_UPDATE_BRANCH=main
```

Only use a repository controlled by trusted maintainers. An approved update
runs the repository's installer as root after its tests pass, so super admins
should verify the commit before approving it.

Disable GitHub update checks:

```bash
sudo sed -i 's/^WEBMANAGER_UPDATE_ENABLED=.*/WEBMANAGER_UPDATE_ENABLED=0/' \
  /etc/webmanager/updater.env
sudo systemctl disable --now webmanager-update.timer
sudo systemctl disable --now webmanager-update.path
```

Re-enable them:

```bash
sudo sed -i 's/^WEBMANAGER_UPDATE_ENABLED=.*/WEBMANAGER_UPDATE_ENABLED=1/' \
  /etc/webmanager/updater.env
sudo systemctl enable --now webmanager-update.timer
sudo systemctl enable --now webmanager-update.path
```

### 1. Back up data

Use the backup procedure above before upgrading.

### 2. Update the source checkout

From the original source directory:

```bash
git pull
```

Or replace the source directory with the new release.

### 3. Back up custom dashboard Nginx configuration

If a domain, HTTPS, or custom proxy settings were added:

```bash
sudo cp \
  /etc/nginx/sites-available/webmanager \
  /root/webmanager-nginx-before-upgrade.conf
```

### 4. Run the installer again

```bash
bash setup.sh
```

The installer:

- Replaces application code in `/opt/webmanager`
- Recreates or updates the virtual environment
- Preserves `/var/lib/webmanager`
- Preserves an existing `/etc/webmanager/webmanager.env`
- Preserves an existing `/etc/nginx/sites-available/webmanager`
- Restarts the service

### 5. Verify custom Nginx settings

The installer preserves an existing `/etc/nginx/sites-available/webmanager`. Confirm the active configuration:

```bash
sudo nginx -T | less
```

Compare with the backup if needed, then run:

```bash
sudo nginx -t
sudo systemctl reload nginx
sudo systemctl restart webmanager
```

## Uninstall

Run the installed command from anywhere:

### Remove the application but keep data

```bash
sudo webmanager-uninstall
```

This removes:

- `/opt/webmanager`
- The systemd service
- The dashboard Nginx configuration

It preserves:

```text
/var/lib/webmanager
/etc/webmanager
```

### Remove everything

Warning: this permanently removes users, repositories, configurations, logs, and the database.

```bash
sudo webmanager-uninstall --purge
```

Create and verify a backup first.

The uninstaller does not remove shared Debian packages such as Python, Git, or Nginx.

## Development

### Create a development environment

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### Run tests

```bash
python -m unittest discover -v
```

### Run locally

```bash
python run.py
```

Open:

```text
http://127.0.0.1:5000
```

Without `WEBMANAGER_DATA_DIR`, development data is stored in the source directory's `instance/` folder.

### Development environment variables

Example:

```bash
export WEBMANAGER_DATA_DIR="$PWD/instance"
export WEBMANAGER_HOST=127.0.0.1
export WEBMANAGER_PORT=5000
export WEBMANAGER_SITE_PORT_MIN=8100
export WEBMANAGER_SITE_PORT_MAX=8999
export WEBMANAGER_SITE_GATEWAY_PORT=8090
export WEBMANAGER_SITE_BASE_DOMAIN=webmanager.localhost
export WEBMANAGER_SITE_PUBLIC_SCHEME=http
export WEBMANAGER_NGINX_BINARY=nginx
export WEBMANAGER_GOOGLE_CLIENT_ID="CLIENT_ID.apps.googleusercontent.com"
export WEBMANAGER_GOOGLE_CLIENT_SECRET="CLIENT_SECRET"
export WEBMANAGER_GOOGLE_REDIRECT_URI="http://localhost:5000/auth/google/callback"
export WEBMANAGER_DEBUG=1
python run.py
```

Do not enable debug mode on an Internet-facing server.

## Security checklist

Before exposing WebManager publicly:

1. Enable HTTPS for the dashboard.
2. Set `WEBMANAGER_SESSION_COOKIE_SECURE=1`.
3. Configure Google domain or email allowlists unless every Google account should be allowed.
4. Restrict dashboard access by firewall or VPN when possible.
5. Do not expose internal ports `5000`, `8090`, or `8100-8999`.
6. Use read-only SSH deploy keys for private repositories.
7. Keep Debian, Nginx, Python packages, and WebManager updated.
8. Back up `/var/lib/webmanager`.
9. Store backups away from the server.
10. Review Google OAuth consent-screen and test-user settings before production use.

The Nginx editor enforces each site's hostname, internal ports, document root,
and symlink protection. It also rejects proxy, include, write, module, and
other unsafe directives. Users can still publish files from repositories they
control, so account access should remain limited to trusted people.
