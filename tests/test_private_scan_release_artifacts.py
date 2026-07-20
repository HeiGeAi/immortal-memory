from __future__ import annotations

import io
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import private_scan


def write_zip(path, members: dict[str, str]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)


def write_tar(path, members: dict[str, str], mode: str = "w") -> None:
    with tarfile.open(path, mode) as archive:
        for name, content in members.items():
            payload = content.encode()
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))


@pytest.mark.parametrize(
    ("secret", "rule"),
    [
        ("ou_" + "f85356d9218ccf0cebc7915c0dbc95e5", "lark_open_id"),
        ("Cook" + "ie: session=abcdefghijklmnopqrstuvwxyz", "cookie_header"),
        ("Authorization: Bea" + "rer abcdefghijklmnopqrstuvwxyz", "bearer_token"),
        ("https://user:pass" + "word@example.com/path", "url_userinfo"),
        ("AKIA" + "IOSFODNN7EXAMPLE", "aws_access_key"),
        ("/Us" + "ers/private-owner/.immortal/index.jsonl", "absolute_home_path"),
        ("/ho" + "me/private-owner/.immortal/index.jsonl", "absolute_home_path"),
        ("C:\\Us" + "ers\\private-owner\\.immortal\\index.jsonl", "absolute_home_path"),
        ("-----BEGIN PRIVATE " + "KEY-----\nredacted\n-----END PRIVATE KEY-----", "private_key_block"),
    ],
)
def test_private_scan_detects_archive_secrets_without_reporting_values(tmp_path, secret, rule):
    artifact = tmp_path / "artifact.zip"
    write_zip(artifact, {"nested/payload.txt": secret})

    result = private_scan.scan_paths([artifact])
    dumped = repr(result)

    assert result["hits"]
    assert result["hits"][0]["member"] == "nested/payload.txt"
    assert rule in {hit["rule"] for hit in result["hits"]}
    assert secret not in dumped


@pytest.mark.parametrize(
    ("suffix", "writer"),
    [
        (".whl", lambda path: write_zip(path, {"pkg/data.txt": "Cook" + "ie: abcdefghijklmnop"})),
        (".zip", lambda path: write_zip(path, {"pkg/data.txt": "Cook" + "ie: abcdefghijklmnop"})),
        (".tar", lambda path: write_tar(path, {"pkg/data.txt": "Cook" + "ie: abcdefghijklmnop"})),
        (
            ".tar.gz",
            lambda path: write_tar(path, {"pkg/data.txt": "Cook" + "ie: abcdefghijklmnop"}, "w:gz"),
        ),
    ],
)
def test_supported_release_archive_types_are_scanned(tmp_path, suffix, writer):
    artifact = tmp_path / f"artifact{suffix}"
    writer(artifact)

    result = private_scan.scan_paths([artifact])

    assert any(hit["rule"] == "cookie_header" for hit in result["hits"])


def test_tar_traversal_member_is_never_extracted_and_is_reported_safely(tmp_path):
    artifact = tmp_path / "artifact.tar"
    outside = tmp_path / "escaped.txt"
    write_tar(artifact, {"../../escaped.txt": "Authorization: Bea" + "rer abcdefghijklmnop"})

    result = private_scan.scan_paths([artifact])

    assert not outside.exists()
    assert result["hits"][0]["member"] == "../../escaped.txt"
    assert result["hits"][0]["rule"] == "bearer_token"


def test_clean_archive_has_no_hits(tmp_path):
    artifact = tmp_path / "clean.zip"
    write_zip(artifact, {"README.txt": "public release notes"})

    assert private_scan.scan_paths([artifact])["hits"] == []
