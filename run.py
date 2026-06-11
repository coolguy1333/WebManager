from webmanager import create_app


app = create_app()


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
