"""Typed, import-backed capability checks for the v1.1 full pipeline."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Optional


@dataclass(frozen=True)
class PipelineCapability:
    stage_id: str
    command: str
    module_filename: str
    host_handler_name: str
    compatibility_filename: Optional[str] = None


@dataclass(frozen=True)
class CapabilityStatus:
    stage_id: str
    ready: bool
    reasons: tuple[str, ...]


PIPELINE_CAPABILITIES = (
    PipelineCapability("run", "run", "immortal.py", "command_run"),
    PipelineCapability(
        "claims-migrate",
        "claims-migrate",
        "model_migration.py",
        "command_claims_migrate",
    ),
    PipelineCapability(
        "profile-attribution-audit",
        "profile-attribution-audit",
        "profile_attribution_audit.py",
        "command_profile_attribution_audit",
    ),
    PipelineCapability(
        "living-self-build",
        "living-self-build",
        "living_self_service.py",
        "command_living_self_build",
    ),
    PipelineCapability(
        "cards-build",
        "cards",
        "judgment_store.py",
        "command_cards_build",
        compatibility_filename="cards.py",
    ),
    PipelineCapability(
        "context-preview",
        "context-preview",
        "context_compiler.py",
        "command_context_preview",
    ),
)


def _load_module(path: Path, purpose: str) -> Optional[ModuleType]:
    if not path.is_file():
        return None
    digest = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:12]
    module_name = f"_immortal_capability_{purpose}_{digest}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    inserted = False
    try:
        root = str(path.parent)
        if root not in sys.path:
            sys.path.insert(0, root)
            inserted = True
        spec.loader.exec_module(module)
        return module
    except Exception:
        return None
    finally:
        if inserted:
            try:
                sys.path.remove(root)
            except ValueError:
                pass
        sys.modules.pop(module_name, None)


def _subparser_handlers(parser: argparse.ArgumentParser) -> dict[str, object]:
    handlers: dict[str, object] = {}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, subparser in action.choices.items():
                handlers[str(name)] = subparser.get_default("func")
    return handlers


def _callable_export(module: ModuleType, stage_id: str) -> bool:
    exports = getattr(module, "PIPELINE_CAPABILITIES", None)
    return isinstance(exports, dict) and callable(exports.get(stage_id))


def pipeline_capability_status(skill_dir: Path) -> tuple[CapabilityStatus, ...]:
    root = Path(skill_dir)
    host = _load_module(root / "immortal.py", "host")
    parser = None
    if host is not None:
        build_parser = getattr(host, "build_parser", None)
        if callable(build_parser):
            try:
                parser = build_parser()
            except Exception:
                parser = None
    handlers = (
        _subparser_handlers(parser)
        if isinstance(parser, argparse.ArgumentParser)
        else {}
    )

    statuses: list[CapabilityStatus] = []
    for capability in PIPELINE_CAPABILITIES:
        reasons: list[str] = []
        if capability.command not in handlers:
            reasons.append("subparser_missing")
        else:
            handler = handlers[capability.command]
            if not callable(handler):
                reasons.append("host_handler_missing")
            elif getattr(handler, "__name__", "") != capability.host_handler_name:
                reasons.append("host_handler_mismatch")

        if capability.module_filename == "immortal.py":
            module = host
        else:
            module = _load_module(
                root / capability.module_filename,
                capability.stage_id.replace("-", "_"),
            )
        if module is None:
            reasons.append(
                "module_missing"
                if not (root / capability.module_filename).is_file()
                else "module_import_failed"
            )
        elif not _callable_export(module, capability.stage_id):
            reasons.append("capability_not_callable")

        if capability.compatibility_filename:
            compatibility_path = root / capability.compatibility_filename
            compatibility = _load_module(
                compatibility_path,
                capability.stage_id.replace("-", "_") + "_compat",
            )
            if compatibility is None:
                reasons.append(
                    "compatibility_missing"
                    if not compatibility_path.is_file()
                    else "compatibility_import_failed"
                )
            elif (
                getattr(compatibility, "CAPABILITY_READY", None) is not True
                or not _callable_export(compatibility, capability.stage_id)
            ):
                reasons.append("compatibility_not_ready")

        statuses.append(
            CapabilityStatus(
                stage_id=capability.stage_id,
                ready=not reasons,
                reasons=tuple(reasons),
            )
        )
    return tuple(statuses)


def missing_pipeline_stages(skill_dir: Path) -> tuple[str, ...]:
    return tuple(
        status.stage_id
        for status in pipeline_capability_status(skill_dir)
        if not status.ready
    )
