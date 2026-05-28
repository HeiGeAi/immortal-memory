#!/usr/bin/env python3
"""Runtime configuration for the Immortal skill.

The skill code is generic; the local vault configuration binds it to a real
person, accounts, and allowed data sources. Keep private identity values in
~/.immortal/config.json instead of hardcoding them into distributable code.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


HOME = Path.home()
DEFAULT_VAULT_DIR = HOME / ".immortal"
CONFIG_FILE = DEFAULT_VAULT_DIR / "config.json"


DEFAULT_CONFIG: dict[str, Any] = {
    "version": 1,
    "owner_name": "",
    "owner_display_name": "",
    "owner_aliases": [],
    "primary_account": "",
    "account_contexts": {},
    "vault_dir": str(DEFAULT_VAULT_DIR),
    "feishu": {
        "expected_user_name": "",
        "expected_user_open_id": "",
        "reject_user_names": [],
        "daily_sources": "contacts,chats,members,messages,message-search,tasks,calendar,vc,minutes,docs,doc-contents",
    },
    "role_defaults": {
        "goal": "写稿审稿流程",
        "mode": "auto",
        "slug_prefix": "user",
    },
    "automation": {
        "daily_launch_agent_label": "",
        "daily_schedule": [
            {"hour": 9, "minute": 7},
            {"hour": 13, "minute": 23},
            {"hour": 20, "minute": 17},
            {"hour": 3, "minute": 3},
        ],
    },
    "extra_sources": [],
}


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def load_config(path: Path | None = None) -> dict[str, Any]:
    config_path = path or CONFIG_FILE
    raw = read_json(config_path, {})
    if not isinstance(raw, dict):
        raw = {}
    config = deep_merge(DEFAULT_CONFIG, raw)
    vault_dir = str(config.get("vault_dir") or DEFAULT_VAULT_DIR)
    config["vault_dir"] = str(Path(vault_dir).expanduser())
    return config


def save_config(config: dict[str, Any], path: Path | None = None) -> Path:
    config_path = path or CONFIG_FILE
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return config_path


def configured_vault_dir(config: dict[str, Any] | None = None) -> Path:
    loaded = config or load_config()
    return Path(str(loaded.get("vault_dir") or DEFAULT_VAULT_DIR)).expanduser()


def owner_display_name(config: dict[str, Any] | None = None) -> str:
    loaded = config or load_config()
    return str(loaded.get("owner_display_name") or loaded.get("owner_name") or "the configured user").strip()


def owner_aliases(config: dict[str, Any] | None = None) -> list[str]:
    loaded = config or load_config()
    aliases = loaded.get("owner_aliases") or []
    if not isinstance(aliases, list):
        aliases = []
    names = [loaded.get("owner_name"), loaded.get("owner_display_name"), *aliases]
    result: list[str] = []
    seen: set[str] = set()
    for item in names:
        value = re.sub(r"\s+", " ", str(item or "")).strip()
        if len(value) < 2:
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def slug_prefix(config: dict[str, Any] | None = None) -> str:
    loaded = config or load_config()
    value = str((loaded.get("role_defaults") or {}).get("slug_prefix") or "user").strip().lower()
    value = re.sub(r"[^a-z0-9-]+", "-", value).strip("-")
    return value or "user"


def feishu_guard_args(config: dict[str, Any] | None = None) -> list[str]:
    loaded = config or load_config()
    feishu = loaded.get("feishu") if isinstance(loaded.get("feishu"), dict) else {}
    args: list[str] = []
    expected_name = str(feishu.get("expected_user_name") or "").strip()
    expected_open_id = str(feishu.get("expected_user_open_id") or "").strip()
    if expected_name:
        args.extend(["--expected-user-name", expected_name])
    if expected_open_id:
        args.extend(["--expected-user-open-id", expected_open_id])
    reject_names = feishu.get("reject_user_names") or []
    if isinstance(reject_names, list):
        for name in reject_names:
            value = str(name or "").strip()
            if value:
                args.extend(["--reject-user-name", value])
    return args


def feishu_daily_sources(config: dict[str, Any] | None = None) -> str:
    loaded = config or load_config()
    feishu = loaded.get("feishu") if isinstance(loaded.get("feishu"), dict) else {}
    return str(feishu.get("daily_sources") or DEFAULT_CONFIG["feishu"]["daily_sources"])


def daily_launch_agent_label(config: dict[str, Any] | None = None) -> str:
    loaded = config or load_config()
    automation = loaded.get("automation") if isinstance(loaded.get("automation"), dict) else {}
    configured = str(automation.get("daily_launch_agent_label") or "").strip()
    if configured:
        return configured
    prefix = slug_prefix(loaded)
    return f"com.{prefix}.immortal.daily-backup" if prefix != "user" else "com.immortal.daily-backup"


def daily_schedule(config: dict[str, Any] | None = None) -> list[dict[str, int]]:
    loaded = config or load_config()
    automation = loaded.get("automation") if isinstance(loaded.get("automation"), dict) else {}
    raw = automation.get("daily_schedule") or DEFAULT_CONFIG["automation"]["daily_schedule"]
    if not isinstance(raw, list):
        raw = DEFAULT_CONFIG["automation"]["daily_schedule"]
    result: list[dict[str, int]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            hour = int(item.get("hour"))
            minute = int(item.get("minute"))
        except (TypeError, ValueError):
            continue
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            result.append({"hour": hour, "minute": minute})
    return result or list(DEFAULT_CONFIG["automation"]["daily_schedule"])
