import sys

from webmanager import StartupError, create_app

if len(sys.argv) > 1 and sys.argv[1] == "restore":
    # `sudo webmanager-restore FILE` (WebManager must be stopped): put a backup's
    # database and keys in place. Sources are cloned again when it next starts.
    import os
    from pathlib import Path

    from webmanager.backups import BackupError, restore_backup

    if len(sys.argv) != 3:
        print("Usage: run.py restore BACKUP.tar.gz", file=sys.stderr)
        raise SystemExit(2)
    data_dir = Path(
        os.environ.get("WEBMANAGER_DATA_DIR") or Path(__file__).resolve().parent / "instance"
    ).expanduser().resolve()
    try:
        outcome = restore_backup(Path(sys.argv[2]), data_dir, data_dir / "webmanager.sqlite3")
    except BackupError as exc:
        print(f"Restore failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    made = outcome["manifest"].get("created_at") or "an unknown time"
    print(f"Restored the backup made at {made} UTC.")
    if outcome["previous"]:
        print(f"The database it replaced is kept as {outcome['previous']}.")
    if outcome["settings"]:
        print(f"The backup's settings file was saved as {outcome['settings']}.")
    raise SystemExit(0)

if len(sys.argv) > 1 and sys.argv[1] == "doctor":
    # `sudo webmanager-doctor`: check everything WebManager depends on, without
    # starting it (and without needing to be able to sign in).
    from webmanager.health import run_doctor

    try:
        raise SystemExit(run_doctor())
    except StartupError as exc:
        print(f"WebManager cannot start: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


try:
    app = create_app()
except StartupError as exc:
    # A configuration problem: say what to fix, not where in the code it failed.
    print(f"WebManager cannot start: {exc}", file=sys.stderr)
    raise SystemExit(1) from None


if __name__ == "__main__":
    if app.config["DEBUG"]:
        app.run(
            host=app.config["HOST"],
            port=app.config["PORT"],
            debug=True,
            use_reloader=False,
        )
    else:
        try:
            from waitress import serve
        except ModuleNotFoundError:
            print("Waitress is not installed; using Flask's development server.")
            app.run(
                host=app.config["HOST"],
                port=app.config["PORT"],
                use_reloader=False,
            )
        else:
            serve(app, host=app.config["HOST"], port=app.config["PORT"], threads=8)
