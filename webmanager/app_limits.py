"""How much memory, CPU and how many processes each app container may use.

An app's limits start from defaults (its ``webmanager.json`` ``memory_mb``, the
server-wide CPU and process defaults) and can be changed per app on its
Resources page. A blank value means "use the default", so a later change to the
default (or to the manifest) still reaches apps nobody customised.

People who manage an app may set it up to the server's owner caps; super
admins up to what the machine physically has.
"""

import math
import os
from pathlib import Path

from . import apps as app_support, system_metrics

MIB = 1024 * 1024
# What an owner may give an app unless the admin raised the caps.
DEFAULT_OWNER_MAX_MEMORY_MB = app_support.MAX_MEMORY_MB
DEFAULT_OWNER_MAX_CPUS = 2.0
OWNER_MAX_PIDS = 1024


def host_capacity() -> dict:
    """What this machine has: {"memory_mb": int | None, "cpus": int}."""
    memory = system_metrics.memory()
    return {
        "memory_mb": int(memory["total"] // MIB) if memory else None,
        "cpus": os.cpu_count() or 1,
    }


def caps(config, is_admin: bool) -> dict:
    """The most a person may give one app: {"memory_mb", "cpus", "pids"}."""
    host = host_capacity()
    host_memory = host["memory_mb"] or app_support.MAX_MEMORY_MB
    if is_admin:
        return {"memory_mb": host_memory, "cpus": float(host["cpus"]), "pids": app_support.MAX_PIDS}
    return {
        "memory_mb": max(
            app_support.MIN_MEMORY_MB,
            min(int(config.get("APP_MAX_MEMORY_MB") or DEFAULT_OWNER_MAX_MEMORY_MB), host_memory),
        ),
        "cpus": max(
            app_support.MIN_CPUS,
            min(float(config.get("APP_MAX_CPUS") or DEFAULT_OWNER_MAX_CPUS), float(host["cpus"])),
        ),
        "pids": OWNER_MAX_PIDS,
    }


def defaults(config, manifest_memory_mb) -> dict:
    """What an app gets when nothing was customised. An app's own manifest can
    ask for no more than the owner cap, or the cap would mean nothing."""
    owner_cap = int(config.get("APP_MAX_MEMORY_MB") or DEFAULT_OWNER_MAX_MEMORY_MB)
    return {
        "memory_mb": max(
            app_support.MIN_MEMORY_MB,
            min(int(manifest_memory_mb or config["APP_DEFAULT_MEMORY_MB"]), owner_cap),
        ),
        "cpus": float(config["APP_CPUS"]),
        "pids": app_support.DEFAULT_PIDS_LIMIT,
    }


def effective(site, config, manifest_memory_mb=None) -> dict:
    """The limits an app's container runs with. CPUs never exceed the
    machine's (a replica or restored copy may be smaller than where the app
    was set up, and the container runtime refuses more CPUs than exist)."""
    base = defaults(config, manifest_memory_mb)
    return {
        "memory_mb": int(site["app_memory_mb"] or base["memory_mb"]),
        "cpus": min(float(site["app_cpus"] or base["cpus"]), float(host_capacity()["cpus"])),
        "pids": int(site["app_pids"] or base["pids"]),
    }


def _number(raw: str, label: str, integer: bool):
    text = (raw or "").strip().replace(",", ".")
    if text == "":
        return None
    try:
        value = int(text) if integer else float(text)
    except ValueError:
        raise ValueError(f"{label} must be a {'whole ' if integer else ''}number.") from None
    if not math.isfinite(value):
        raise ValueError(f"{label} must be a {'whole ' if integer else ''}number.")
    return value


def parse_form(form, current, limits: dict) -> tuple[dict, list[str]]:
    """Read the Resources form. Returns ({"memory_mb", "cpus", "pids"} where
    None means "use the default"}, [problems]).

    ``current`` holds the values already saved: one that is unchanged is always
    accepted, even above today's cap (an admin may have set it higher)."""
    values: dict = {}
    problems: list[str] = []

    def read(field, label, integer, minimum, maximum, unit, saved_key):
        try:
            number = _number(form.get(field, ""), label, integer)
        except ValueError as exc:
            problems.append(str(exc))
            values[saved_key] = current[saved_key]
            return
        if number is not None and not integer:
            number = round(number, 2)
        saved = current[saved_key]
        if number is not None and number != saved and not (minimum <= number <= maximum):
            problems.append(f"{label} must be between {minimum:g} and {maximum:g}{unit}.")
            values[saved_key] = saved
            return
        values[saved_key] = number

    read("memory_mb", "Memory", True, app_support.MIN_MEMORY_MB, limits["memory_mb"], " MB", "memory_mb")
    read("cpus", "CPU", False, app_support.MIN_CPUS, limits["cpus"], " CPUs", "cpus")
    read("pids", "Process limit", True, app_support.MIN_PIDS, limits["pids"], "", "pids")
    return values, problems


def manifest_memory(site, config):
    """The memory an app's own webmanager.json asks for (None if unreadable)."""
    try:
        return app_support.load_manifest(
            Path(site["document_root"]), config["APP_DEFAULT_MEMORY_MB"]
        )["memory_mb"]
    except (app_support.AppError, OSError):
        return None


def allocation(sites, config) -> dict:
    """Added-up limits of these apps, against what the machine has."""
    memory = 0
    cpus = 0.0
    for site in sites:
        limits = effective(site, config, manifest_memory(site, config))
        memory += limits["memory_mb"]
        cpus += limits["cpus"]
    host = host_capacity()
    return {
        "apps": len(sites),
        "memory_mb": memory,
        "cpus": round(cpus, 2),
        "host_memory_mb": host["memory_mb"],
        "host_cpus": host["cpus"],
        "memory_over": bool(host["memory_mb"] and memory > host["memory_mb"]),
        "cpus_over": cpus > host["cpus"],
    }
