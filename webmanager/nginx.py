import re
from pathlib import Path


BLOCKED_DIRECTIVES = {
    "access_log",
    "alias",
    "auth_basic_user_file",
    "client_body_in_file_only",
    "client_body_temp_path",
    "create_full_put_path",
    "daemon",
    "dav_methods",
    "env",
    "error_log",
    "include",
    "load_module",
    "master_process",
    "pid",
    "proxy_store",
    "proxy_store_access",
    "user",
    "worker_processes",
}


class NginxConfigError(ValueError):
    pass


def nginx_path(path: str | Path) -> str:
    return Path(path).resolve().as_posix().replace('"', '\\"')


def build_site_config(
    site_name: str,
    document_root: Path,
    index_file: str,
    port: int,
    spa_fallback: bool,
    hostname: str | None = None,
    gateway_port: int | None = None,
) -> str:
    fallback = f"/{index_file}" if spa_fallback else "=404"
    if hostname and gateway_port:
        listeners = (
            f"    listen 127.0.0.1:{port};\n"
            f"    listen [::1]:{port};\n"
            f"    listen 127.0.0.1:{gateway_port};\n"
            f"    listen [::1]:{gateway_port};"
        )
        server_name = hostname
    else:
        listeners = f"    listen {port};\n    listen [::]:{port};"
        server_name = "_"
    return f"""# Managed by WebManager for {site_name}
server {{
{listeners}
    server_name {server_name};

    root "{nginx_path(document_root)}";
    index {index_file};
    disable_symlinks on;

    location / {{
        try_files $uri $uri/ {fallback};
    }}

    location ~ /\\. {{
        deny all;
    }}

    add_header X-Content-Type-Options "nosniff" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;
}}
"""


def build_main_config(
    prefix: Path,
    config_directory: Path,
    gateway_port: int | None = None,
) -> str:
    prefix_path = nginx_path(prefix)
    config_path = nginx_path(config_directory / "*.conf")
    gateway_default = ""
    if gateway_port:
        gateway_default = f"""
    server {{
        listen 127.0.0.1:{gateway_port} default_server;
        listen [::1]:{gateway_port} default_server;
        server_name _;
        return 404;
    }}
"""
    return f"""worker_processes auto;
pid "{prefix_path}/nginx.pid";
error_log "{prefix_path}/error.log";

events {{
    worker_connections 1024;
}}

http {{
    default_type application/octet-stream;
    types {{
        text/html html htm;
        text/css css;
        text/plain txt;
        application/javascript js mjs;
        application/json json;
        application/wasm wasm;
        image/svg+xml svg;
        image/png png;
        image/jpeg jpg jpeg;
        image/gif gif;
        image/webp webp;
        image/x-icon ico;
        font/woff woff;
        font/woff2 woff2;
    }}
    access_log "{prefix_path}/access.log";
    client_body_temp_path "{prefix_path}/temp/client_body";
    proxy_temp_path "{prefix_path}/temp/proxy";
    fastcgi_temp_path "{prefix_path}/temp/fastcgi";
    uwsgi_temp_path "{prefix_path}/temp/uwsgi";
    scgi_temp_path "{prefix_path}/temp/scgi";
    sendfile on;
    keepalive_timeout 65;
{gateway_default}
    include "{config_path}";
}}
"""


def config_uses_port(config: str, port: int) -> bool:
    return bool(re.search(rf"(?m)^\s*listen\s+(?:\[[^\]]+\]:)?{port}\b", config))


def route_site_config(
    config: str,
    port: int,
    hostname: str,
    gateway_port: int,
) -> str:
    routed = re.sub(
        r"(?m)^[ \t]*listen[ \t]+[^;\r\n]+;[ \t]*(?:\r?\n)?",
        "",
        config,
    )
    listeners = (
        f"    listen 127.0.0.1:{port};\n"
        f"    listen [::1]:{port};\n"
        f"    listen 127.0.0.1:{gateway_port};\n"
        f"    listen [::1]:{gateway_port};\n"
    )
    routed, count = re.subn(
        r"(?m)^\s*server_name\s+[^;]+;",
        f"    server_name {hostname};",
        routed,
        count=1,
    )
    if count == 0:
        routed = re.sub(
            r"(?m)^(\s*server\s*\{\s*)$",
            rf"\1\n    server_name {hostname};",
            routed,
            count=1,
        )
    return re.sub(
        r"(?m)^(\s*server\s*\{\s*)$",
        rf"\1\n{listeners.rstrip()}",
        routed,
        count=1,
    )


def validate_site_config(
    config: str,
    document_root: str | Path,
    port: int,
    hostname: str | None = None,
    gateway_port: int | None = None,
):
    directives = _parse_directives(_tokenize(config))
    if len(directives) != 1 or directives[0][0].lower() != "server" or directives[0][2] is None:
        raise NginxConfigError("Configuration must contain exactly one server block.")

    expected_root = Path(document_root).resolve()
    server_directives = directives[0][2]
    server_roots = [
        arguments
        for name, arguments, nested in server_directives
        if name.lower() == "root" and nested is None
    ]
    if not server_roots or any(
        len(arguments) != 1 or Path(arguments[0]).resolve() != expected_root
        for arguments in server_roots
    ):
        raise NginxConfigError("The server block must keep the assigned document root.")

    server_symlink_guards = [
        arguments
        for name, arguments, nested in server_directives
        if name.lower() == "disable_symlinks" and nested is None
    ]
    if not server_symlink_guards or any(
        not arguments or arguments[0].lower() != "on"
        for arguments in server_symlink_guards
    ):
        raise NginxConfigError("The server block must keep disable_symlinks on.")

    seen_ports = set()
    allowed_ports = {port}
    if hostname and gateway_port:
        allowed_ports.add(gateway_port)
        server_names = [
            arguments
            for name, arguments, nested in server_directives
            if name.lower() == "server_name" and nested is None
        ]
        if server_names != [[hostname]]:
            raise NginxConfigError(
                f"The server block must keep hostname {hostname}."
            )

    def inspect(children, inside_server=True):
        for name, arguments, nested in children:
            directive = name.lower()
            if directive == "server" and inside_server:
                raise NginxConfigError("Nested or additional server blocks are not allowed.")
            if (
                directive in BLOCKED_DIRECTIVES
                or directive.endswith("_pass")
                or directive.startswith(("perl", "js_"))
                or "lua" in directive
                or directive.startswith("ssl_")
            ):
                raise NginxConfigError(f"The {name} directive is not allowed in managed configs.")

            if directive == "listen":
                listen_port = _listen_port(arguments[0]) if arguments else None
                if listen_port not in allowed_ports:
                    expected = " or ".join(str(value) for value in sorted(allowed_ports))
                    raise NginxConfigError(
                        f"Every listen directive must use managed port {expected}."
                    )
                seen_ports.add(listen_port)
            elif directive == "root":
                if len(arguments) != 1 or Path(arguments[0]).resolve() != expected_root:
                    raise NginxConfigError("The root directive must keep the assigned document root.")
            elif directive == "disable_symlinks":
                if not arguments or arguments[0].lower() != "on":
                    raise NginxConfigError("disable_symlinks must remain enabled.")

            if nested is not None:
                inspect(nested)

    inspect(directives[0][2])
    if port not in seen_ports:
        raise NginxConfigError(f"Configuration must listen on assigned port {port}.")
    if gateway_port and gateway_port not in seen_ports:
        raise NginxConfigError(
            f"Configuration must listen on gateway port {gateway_port}."
        )


def _listen_port(endpoint: str) -> int | None:
    if endpoint.isdigit():
        return int(endpoint)
    match = re.search(r":(\d+)$", endpoint)
    return int(match.group(1)) if match else None


def _tokenize(config: str) -> list[str]:
    tokens = []
    index = 0
    while index < len(config):
        character = config[index]
        if character.isspace():
            index += 1
            continue
        if character == "#":
            newline = config.find("\n", index)
            index = len(config) if newline == -1 else newline + 1
            continue
        if character in "{};":
            tokens.append(character)
            index += 1
            continue
        if character in {'"', "'"}:
            quote = character
            index += 1
            value = []
            while index < len(config) and config[index] != quote:
                if config[index] == "\\" and index + 1 < len(config):
                    index += 1
                value.append(config[index])
                index += 1
            if index >= len(config):
                raise NginxConfigError("Configuration contains an unterminated quoted value.")
            tokens.append("".join(value))
            index += 1
            continue

        value = []
        while index < len(config) and not config[index].isspace() and config[index] not in "{};#":
            if config[index] == "\\" and index + 1 < len(config):
                index += 1
            value.append(config[index])
            index += 1
        if value:
            tokens.append("".join(value))
    return tokens


def _parse_directives(tokens: list[str], start: int = 0, nested: bool = False):
    directives = []
    header = []
    index = start
    while index < len(tokens):
        token = tokens[index]
        if token == "}":
            if not nested or header:
                raise NginxConfigError("Configuration contains an unexpected closing brace.")
            return directives, index + 1
        if token == ";":
            if not header:
                raise NginxConfigError("Configuration contains an empty directive.")
            directives.append((header[0], header[1:], None))
            header = []
            index += 1
            continue
        if token == "{":
            if not header:
                raise NginxConfigError("Configuration contains an unnamed block.")
            children, index = _parse_directives(tokens, index + 1, nested=True)
            directives.append((header[0], header[1:], children))
            header = []
            continue
        header.append(token)
        index += 1

    if nested:
        raise NginxConfigError("Configuration contains an unclosed block.")
    if header:
        raise NginxConfigError("The final directive is missing a semicolon.")
    return directives
