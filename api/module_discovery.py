"""
Discovery for community modules' WebUI integration.

Core modules (storage, network, shares, users, updates) keep their routes,
templates, and JS in this repo exactly as before — api/main.py registers
them directly, unchanged. A community module (e.g. nextcloud, docker) is
different: it ships its own api_routes.py + frontend/ inside its
modules/<id>/ directory (usually its own repo/package, dropped alongside
freedomnas-core's modules — see freedomnas-core's module_loader.py for the
equivalent runtime-module discovery). This scans that same modules
directory for manifest.json entries with a non-null "webui" block and loads
what's needed to mount them, without touching any of this repo's own
routes or templates.
"""

from __future__ import annotations

import importlib.util
import json
import logging
from pathlib import Path
from typing import List, NamedTuple, Optional

from fastapi import APIRouter

logger = logging.getLogger("FreedomNAS.WebUI.ModuleDiscovery")


class NavEntry(NamedTuple):
    href: str
    label: str
    icon: str
    level_class: str


class CommunityModule(NamedTuple):
    id: str
    router: object
    route_prefix: str
    templates_dir: Optional[Path]
    static_dir: Optional[Path]
    page_route: Optional[str]
    page_template: Optional[str]
    nav: Optional[NavEntry] = None


_LEVEL_CLASSES = {"assisted": "", "advanced": "advanced-only", "expert": "expert-only"}


def _nav_entry(module_id: str, webui: dict, static_dir: Optional[Path]) -> Optional[NavEntry]:
    """
    The sidebar entry a module asks for with webui.nav: {label, icon, level}.
    `icon` is "<file>#<symbol>" inside the module's static_dir; without one
    the generic module icon is used. `level` is the lowest UI level that
    shows the entry. Only a module with a page of its own gets one.
    """
    nav = webui.get("nav")
    page_route = webui.get("page_route")
    if nav is None or not page_route or not webui.get("page_template"):
        return None
    level = nav.get("level", "assisted")
    if level not in _LEVEL_CLASSES:
        logger.warning("Module '%s': unknown nav level '%s' — shown from Advanced", module_id, level)
        level = "advanced"
    icon = "#icon-module"
    if nav.get("icon") and static_dir:
        icon = f"/static/modules/{module_id}/{nav['icon']}"
    return NavEntry(
        href=page_route,
        label=nav.get("label") or module_id,
        icon=icon,
        level_class=_LEVEL_CLASSES[level],
    )


def _default_modules_dir() -> Path:
    """Mirrors freedomnas-core's ModuleLoader._get_default_modules_dir()."""
    here = Path(__file__).resolve().parent            # <install_root>/webui/api
    candidates = [
        here.parent.parent / "modules",                # system install
        here.parent.parent / "freedomnas-core" / "modules",  # dev sibling repo
    ]
    for path in candidates:
        if path.exists() and path.is_dir():
            return path
    return candidates[0]


class FailedModule(NamedTuple):
    id: str
    reason: str


failed_modules: List[FailedModule] = []
"""The community modules the last discovery could not load, with the reason."""


class ModuleLoadError(Exception):
    pass


def discover_community_modules(modules_dir: Optional[str] = None) -> List[CommunityModule]:
    """
    Scan modules_dir for manifest.json entries with a "webui" block, load
    each one's api_routes.py by file path, and return what's needed to
    mount it. Modules with no "webui" block (all core modules today) are
    silently skipped — their WebUI integration is handled the existing way.

    Each module is loaded on its own: one that cannot be loaded is left out
    and recorded in failed_modules, and the others are still returned.
    """
    failed_modules.clear()
    base = Path(modules_dir) if modules_dir else _default_modules_dir()
    if not base.exists():
        logger.debug("Modules directory not found: %s — no community modules to mount", base)
        return []

    found: List[CommunityModule] = []
    for entry in sorted(base.iterdir()):
        if not entry.is_dir() or entry.name.startswith(("_", ".")):
            continue  # e.g. modules/_template/ — scaffolding, never a real module
        manifest_path = entry / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            module = _load_module(entry, manifest_path)
        except ModuleLoadError as exc:
            _fail(entry.name, str(exc))
            continue
        except Exception as exc:
            _fail(entry.name, f"{type(exc).__name__}: {exc}")
            continue
        if module is not None:
            found.append(module)
            logger.info("Discovered community module '%s' for WebUI mounting", module.id)
    return found


def _fail(module_id: str, reason: str) -> None:
    logger.error("Module '%s' was not loaded: %s", module_id, reason)
    failed_modules.append(FailedModule(module_id, reason))


def _load_module(entry: Path, manifest_path: Path) -> Optional[CommunityModule]:
    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as exc:
        raise ModuleLoadError(f"invalid JSON in {manifest_path}, line {exc.lineno} column {exc.colno}: {exc.msg}")
    if not isinstance(manifest, dict):
        raise ModuleLoadError(f"{manifest_path} is not a JSON object")

    webui = manifest.get("webui")
    if not webui:
        return None
    module_id = manifest.get("id", entry.name)
    if not isinstance(webui, dict) or not webui.get("api_routes"):
        raise ModuleLoadError(f"the webui block of {manifest_path} names no api_routes")

    routes_file = entry / webui["api_routes"]
    if not routes_file.exists():
        raise ModuleLoadError(f"webui.api_routes names {routes_file}, which is missing")

    spec = importlib.util.spec_from_file_location(
        f"freedomnas_webui_module_{module_id}", routes_file
    )
    if spec is None or spec.loader is None:
        raise ModuleLoadError(f"{routes_file} cannot be loaded")
    py_module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(py_module)
    except SyntaxError as exc:
        raise ModuleLoadError(f"SyntaxError in {exc.filename}, line {exc.lineno}: {exc.msg}")

    router_attr = webui.get("router_attr", "router")
    router = getattr(py_module, router_attr, None)
    if not isinstance(router, APIRouter):
        raise ModuleLoadError(f"{routes_file} has no APIRouter named '{router_attr}'")

    route_prefix = webui.get("route_prefix", f"/api/{module_id}")
    if not isinstance(route_prefix, str) or not route_prefix.startswith("/") or route_prefix.endswith("/"):
        raise ModuleLoadError(f"webui.route_prefix {route_prefix!r} must start with / and not end with /")

    templates_dir = entry / webui["templates_dir"] if webui.get("templates_dir") else None
    static_dir = entry / webui["static_dir"] if webui.get("static_dir") else None
    if static_dir and not static_dir.exists():
        static_dir = None

    return CommunityModule(
        id=module_id,
        router=router,
        route_prefix=route_prefix,
        templates_dir=templates_dir if templates_dir and templates_dir.exists() else None,
        static_dir=static_dir,
        page_route=webui.get("page_route"),
        page_template=webui.get("page_template"),
        nav=_nav_entry(module_id, webui, static_dir),
    )
