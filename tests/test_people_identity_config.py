from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_private_identity_config_drives_canonical_names(tmp_path):
    vault = tmp_path / ".immortal"
    vault.mkdir()
    (vault / "config.json").write_text(json.dumps({
        "owner_display_name": "Owner Card",
        "owner_aliases": ["Owner Alias"],
        "people_index": {
            "identities": [
                {
                    "id": "user",
                    "canonical": "Owner Card",
                    "aliases": ["Owner Alias"],
                    "category": "self",
                },
                {
                    "id": "bibi",
                    "canonical": "Partner Card",
                    "aliases": ["Partner Alias"],
                    "category": "business",
                    "text_aliases": ["Partner Mention"],
                },
            ],
            "categories": {"Teammate": "team"},
        },
    }), encoding="utf-8")
    script = """
import json
import people_index
print(json.dumps({
    'owner': people_index.canonical_name('Owner Alias'),
    'partner': people_index.canonical_name('Partner Alias'),
    'owner_category': people_index.CATEGORY_BY_NAME['Owner Card'],
    'partner_category': people_index.CATEGORY_BY_NAME['Partner Card'],
    'teammate_category': people_index.CATEGORY_BY_NAME['Teammate'],
    'text_people': people_index.row_people({'statement': 'Partner Mention joined', 'people': []}),
}))
"""
    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    env["PYTHONPATH"] = str(Path(__file__).parents[1] / "core")
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    payload = json.loads(result.stdout)
    assert payload == {
        "owner": "Owner Card",
        "partner": "Partner Card",
        "owner_category": "self",
        "partner_category": "business",
        "teammate_category": "team",
        "text_people": ["Partner Card"],
    }


def test_owner_config_works_without_private_identity_rules(tmp_path):
    vault = tmp_path / ".immortal"
    vault.mkdir()
    (vault / "config.json").write_text(json.dumps({
        "owner_display_name": "Owner Card",
        "owner_aliases": ["Owner Alias"],
    }), encoding="utf-8")
    script = """
import json
import people_index
import quality_report
print(json.dumps({
    'canonical': people_index.canonical_name('Owner Alias'),
    'category': people_index.CATEGORY_BY_NAME['Owner Card'],
    'quality_rule_ids': [rule['id'] for rule in quality_report.CONFIRMED_IDENTITY_RULES],
}))
"""
    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    env["PYTHONPATH"] = str(Path(__file__).parents[1] / "core")
    result = subprocess.run([sys.executable, "-c", script], check=True, capture_output=True, text=True, env=env)

    assert json.loads(result.stdout) == {
        "canonical": "Owner Card",
        "category": "self",
        "quality_rule_ids": ["user"],
    }
