import sys

from webmanager import StartupError, create_app

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
