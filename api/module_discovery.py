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

logger = logging.getLogger("FreedomNAS.WebUI.ModuleDiscovery")


class CommunityModule(NamedTuple):
    id: str
    router: object
    route_prefix: str
    templates_dir: Optional[Path]
    static_dir: Optional[Path]
    page_route: Optional[str]
    page_template: Optional[str]


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


def discover_community_modules(modules_dir: Optional[str] = None) -> List[CommunityModule]:
    """
    Scan modules_dir for manifest.json entries with a "webui" block, load
    each one's api_routes.py by file path, and return what's needed to
    mount it. Modules with no "webui" block (all core modules today) are
    silently skipped — their WebUI integration is handled the existing way.
    """
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
            manifest = json.loads(manifest_path.read_text())
        except json.JSONDecodeError as exc:
            logger.warning("Invalid JSON in %s: %s — skipping", manifest_path, exc)
            continue

        webui = manifest.get("webui")
        if not webui:
            continue

        module_id = manifest.get("id", entry.name)
        routes_file = entry / webui["api_routes"]
        if not routes_file.exists():
            logger.warning(
                "Module '%s' declares webui.api_routes but %s is missing — skipping",
                module_id, routes_file,
            )
            continue

        spec = importlib.util.spec_from_file_location(
            f"freedomnas_webui_module_{module_id}", routes_file
        )
        if spec is None or spec.loader is None:
            logger.warning("Could not load %s for module '%s' — skipping", routes_file, module_id)
            continue
        py_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(py_module)

        router_attr = webui.get("router_attr", "router")
        router = getattr(py_module, router_attr, None)
        if router is None:
            logger.warning(
                "Module '%s': %s has no attribute '%s' — skipping",
                module_id, routes_file, router_attr,
            )
            continue

        templates_dir = entry / webui["templates_dir"] if webui.get("templates_dir") else None
        static_dir = entry / webui["static_dir"] if webui.get("static_dir") else None

        found.append(CommunityModule(
            id=module_id,
            router=router,
            route_prefix=webui.get("route_prefix", f"/api/{module_id}"),
            templates_dir=templates_dir if templates_dir and templates_dir.exists() else None,
            static_dir=static_dir if static_dir and static_dir.exists() else None,
            page_route=webui.get("page_route"),
            page_template=webui.get("page_template"),
        ))
        logger.info("Discovered community module '%s' for WebUI mounting", module_id)

    return found
