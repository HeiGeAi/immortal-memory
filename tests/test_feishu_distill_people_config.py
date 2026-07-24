from __future__ import annotations

import feishu_distill


def test_configured_identity_aliases_are_added_to_people_terms(monkeypatch):
    monkeypatch.setattr(feishu_distill, "load_config", lambda: {
        "people_index": {
            "identities": [{
                "id": "user",
                "canonical": "Owner Card",
                "aliases": ["Owner Alias"],
            }],
            "categories": {"Teammate": "team"},
        },
        "distill": {"people": ["Expert"]},
    })

    terms, identities = feishu_distill.load_configured_people_terms()

    assert terms[:3] == ["Owner Alias", "Teammate", "Expert"]
    assert identities["user"]["canonical"] == "Owner Card"
