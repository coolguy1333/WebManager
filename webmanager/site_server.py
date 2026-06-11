import argparse
import contextlib
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit


class StaticSiteHandler(SimpleHTTPRequestHandler):
    server_version = "WebManagerStatic/1.0"

    def __init__(self, *args, directory=None, index_file="index.html", spa_fallback=False, **kwargs):
        self.index_file = index_file
        self.spa_fallback = spa_fallback
        self.site_root = Path(directory).resolve()
        super().__init__(*args, directory=directory, **kwargs)

    def list_directory(self, path):
        self.send_error(403, "Directory listing is disabled")
        return None

    def send_head(self):
        request_path = unquote(urlsplit(self.path).path)
        requested_path = Path(self.translate_path(request_path)).resolve()
        if requested_path != self.site_root and self.site_root not in requested_path.parents:
            self.send_error(403, "Requested path is outside the site root")
            return None
        if request_path.endswith("/") and requested_path.is_dir():
            configured_index = requested_path / self.index_file
            if configured_index.is_file():
                self.path = f"{request_path}{self.index_file}"
        if self.spa_fallback and not os.path.exists(requested_path):
            self.path = f"/{self.index_file}"
        return super().send_head()

    def log_message(self, format, *args):
        print(f"{self.address_string()} - {format % args}", flush=True)


def serve(root: Path, port: int, index_file: str, spa_fallback: bool):
    handler = lambda *args, **kwargs: StaticSiteHandler(
        *args,
        directory=str(root),
        index_file=index_file,
        spa_fallback=spa_fallback,
        **kwargs,
    )
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()
    server.server_close()


def main():
    parser = argparse.ArgumentParser(description="Serve one WebManager static site.")
    parser.add_argument("--root", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--index", default="index.html")
    parser.add_argument("--spa", action="store_true")
    args = parser.parse_args()
    serve(Path(args.root), args.port, args.index, args.spa)


if __name__ == "__main__":
    main()
