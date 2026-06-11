# Debian deployment files

The complete installation and operations guide is in the project root:

```text
README.md
```

Quick installation from the project root:

```bash
bash setup.sh
```

Setup can then configure HTTPS and Google sign-in. If that step was skipped:

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
| `webmanager.env` | Default production environment |
| `nginx-dashboard.conf` | Dashboard reverse-proxy configuration |

Read the root `README.md` before exposing the service to the Internet. It includes firewall, HTTPS, private repository, backup, upgrade, and troubleshooting instructions.
