# Debian deployment files

The complete installation and operations guide is in the project root:

```text
README.md
```

Quick installation from the project root:

```bash
bash setup.sh
```

Interactive setup immediately collects the Google sign-in and optional HTTPS
settings. For a non-interactive installation, run:

```bash
bash configure-google.sh
```

This directory contains:

| File | Purpose |
| --- | --- |
| `../../setup.sh` | Simple one-command installer |
| `../../uninstall.sh` | Simple one-command uninstaller |
| `../../configure-google.sh` | HTTPS and Google OIDC configuration helper |
| `install.sh` | Installs or upgrades WebManager |
| `uninstall.sh` | Removes WebManager with optional data purge |
| `webmanager.service` | Hardened systemd unit |
| `webmanager-update.service` | Root-only checker and approved-update installer |
| `webmanager-update.timer` | Periodic check-only GitHub polling |
| `webmanager-update.path` | Starts installation after super-admin approval |
| `update.sh` | Check, verify approval, back up data, test, install, and roll back |
| `webmanager-logrotate` | Rotates managed Nginx analytics logs |
| `webmanager.env` | Default production environment |
| `nginx-dashboard.conf` | Dashboard reverse-proxy configuration |

Setup also generates `/etc/nginx/sites-available/webmanager-sites` for wildcard
deployment subdomains. Configure wildcard DNS and wildcard HTTPS coverage for
the dashboard domain before publishing sites.

For Cloudflare Tunnel, route both the dashboard hostname and a wildcard public
hostname such as `*.webmanager.example.com` to `http://localhost:8080`.
WebManager routes the shared tunnel traffic by hostname.

After installation, super administrators can add more deployment domains from
**Admin > Domains**. The page shows the Cloudflare Tunnel hostname, origin,
wildcard DNS record, and verification command required for each domain. No
additional root-owned Nginx edit is needed. Super administrators can also keep
an exact-and-subdomain blocklist there to prevent selected domains from being
used for new assignments.

Sites may use either a subdomain or the domain root. Root hosting requires an
additional Cloudflare Tunnel published hostname and proxied DNS record for the
zone apex; the wildcard record alone does not match it.

Interactive `setup.sh` asks for the initial deployment domain and stores it as
the default. Reinstalls preserve the configured value.

Read the root `README.md` before exposing the service to the Internet. It includes firewall, HTTPS, private repository, backup, upgrade, and troubleshooting instructions.
