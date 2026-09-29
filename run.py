import sys

from webmanager import StartupError, create_app


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
