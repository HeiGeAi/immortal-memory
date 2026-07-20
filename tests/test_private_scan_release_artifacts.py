from __future__ import annotations

import io
import json
import sys
import tarfile
import zipfile
import stat
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
    assert any(hit["member"] == "pkg/data.txt" for hit in result["hits"])


def test_clean_single_file_under_user_directory_shape_does_not_scan_host_parents(
    tmp_path,
):
    artifact = (
        tmp_path
        / "Users"
        / "private-owner"
        / "release"
        / "immortal-clean.zip"
    )
    artifact.parent.mkdir(parents=True)
    write_zip(artifact, {"pkg/data.txt": "clean release content"})

    direct = private_scan.scan_paths([artifact])
    via_parent = private_scan.scan_paths([artifact.parent])

    assert direct["ok"] is True
    assert via_parent["ok"] is True


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


def test_scanner_source_packaged_in_wheel_does_not_self_report_private_literals(tmp_path):
    artifact = tmp_path / "scanner.whl"
    scanner_source = Path(private_scan.__file__).read_text(encoding="utf-8")
    write_zip(artifact, {"immortal_memory/private_scan.py": scanner_source})

    result = private_scan.scan_paths([artifact])

    assert not any(hit["rule"] == "private_identity_marker" for hit in result["hits"])


def test_private_literal_in_another_archive_member_is_still_detected(tmp_path):
    artifact = tmp_path / "artifact.zip"
    marker = "Blake" + " Xu"
    write_zip(artifact, {"payload.txt": marker})

    result = private_scan.scan_paths([artifact])

    assert any(
        hit["member"] == "payload.txt" and hit["rule"] == "private_identity_marker"
        for hit in result["hits"]
    )


def test_small_unknown_extension_inside_directory_is_scanned_as_bytes(tmp_path):
    secret = "AKIA" + "IOSFODNN7EXAMPLE"
    (tmp_path / "payload.dat").write_bytes(secret.encode())

    result = private_scan.scan_paths([tmp_path])

    assert any(hit["rule"] == "aws_access_key" for hit in result["hits"])


def test_conventional_example_home_path_is_not_a_privacy_finding(tmp_path):
    (tmp_path / "fixture.txt").write_text(
        "/Users/" + "example/project", encoding="utf-8"
    )

    result = private_scan.scan_paths([tmp_path])

    assert not any(hit["rule"] == "absolute_home_path" for hit in result["hits"])


@pytest.mark.parametrize("paths", [[], ["/definitely/missing/private-scan-target"]])
def test_empty_or_missing_scan_targets_fail_closed(paths):
    result = private_scan.scan_paths(paths)

    assert result["ok"] is False
    assert result["errors"]


def test_directory_without_files_fails_closed(tmp_path):
    (tmp_path / "empty").mkdir()

    result = private_scan.scan_paths([tmp_path])

    assert result["ok"] is False
    assert any(error["rule"] == "no_files_scanned" for error in result["errors"])


@pytest.mark.parametrize("kind", ["zip", "tar"])
def test_archive_magic_is_detected_after_extension_is_removed(tmp_path, kind):
    secret = "Cook" + "ie: abcdefghijklmnopqrstuvwxyz"
    artifact = tmp_path / "renamed.bin"
    if kind == "zip":
        write_zip(artifact, {"payload.txt": secret})
    else:
        write_tar(artifact, {"payload.txt": secret})

    result = private_scan.scan_paths([artifact])

    assert any(hit["rule"] == "cookie_header" for hit in result["hits"])
    assert any(hit["member"] == "payload.txt" for hit in result["hits"])


def test_encrypted_zip_runtime_error_is_reported_not_raised(tmp_path, monkeypatch):
    artifact = tmp_path / "encrypted.zip"
    write_zip(artifact, {"payload.txt": "clean"})

    def encrypted(*args, **kwargs):
        raise RuntimeError("password required for secret-value")

    monkeypatch.setattr(zipfile.ZipFile, "open", encrypted)
    result = private_scan.scan_paths([artifact])
    dumped = json.dumps(result)

    assert result["ok"] is False
    assert any(error["rule"] == "archive_encrypted_or_unreadable" for error in result["errors"])
    assert "secret-value" not in dumped


def test_sensitive_archive_member_name_is_scanned_but_never_reported_verbatim(tmp_path):
    secret_name = "AKIA" + "IOSFODNN7EXAMPLE" + ".txt"
    artifact = tmp_path / "artifact.zip"
    write_zip(artifact, {secret_name: "clean"})

    result = private_scan.scan_paths([artifact])
    dumped = json.dumps(result)

    hit = next(hit for hit in result["hits"] if hit["rule"] == "aws_access_key")
    assert secret_name not in dumped
    assert hit["member"].startswith("member[")
    assert len(hit["member_sha256_16"]) == 16


def nested_zip_bytes(member_name: str, content: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(member_name, content)
    return buffer.getvalue()


def test_nested_archives_share_one_member_budget(tmp_path, monkeypatch):
    artifact = tmp_path / "outer.zip"
    inner = nested_zip_bytes("payload.txt", "clean")
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr("inner.zip", inner)
    monkeypatch.setattr(private_scan, "MAX_ARCHIVE_MEMBERS", 1)

    result = private_scan.scan_paths([artifact])

    assert any(error["rule"] == "archive_member_limit_exceeded" for error in result["errors"])


def test_nested_archives_share_one_expanded_byte_budget(tmp_path, monkeypatch):
    artifact = tmp_path / "outer.zip"
    inner = nested_zip_bytes("payload.txt", "0123456789")
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr("inner.zip", inner)
    monkeypatch.setattr(private_scan, "MAX_ARCHIVE_BYTES", len(inner) + 5)

    result = private_scan.scan_paths([artifact])

    assert any(error["rule"] == "archive_expanded_size_exceeded" for error in result["errors"])


def test_scan_deadline_is_shared_and_fails_closed(tmp_path):
    (tmp_path / "payload.txt").write_text("clean", encoding="utf-8")

    result = private_scan.scan_paths([tmp_path], deadline_seconds=0)

    assert result["ok"] is False
    assert any(error["rule"] == "scan_deadline_exceeded" for error in result["errors"])


def test_unreadable_file_fails_closed(tmp_path, monkeypatch):
    target = tmp_path / "payload.bin"
    target.write_bytes(b"clean")
    original_open = Path.open

    def denied(self, *args, **kwargs):
        if self == target:
            raise PermissionError("private operating system detail")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied)
    result = private_scan.scan_paths([target])

    assert result["ok"] is False
    assert result["errors"] == [
        {"path": target.name, "member": None, "rule": "file_unreadable"}
    ]


def test_sensitive_regular_filename_is_scanned_without_leaking_name(tmp_path):
    secret_name = "AKIA" + "IOSFODNN7EXAMPLE" + ".txt"
    (tmp_path / secret_name).write_text("clean", encoding="utf-8")

    result = private_scan.scan_paths([tmp_path])
    dumped = json.dumps(result)

    assert secret_name not in dumped
    hit = next(hit for hit in result["hits"] if hit["rule"] == "aws_access_key")
    assert hit["path"].startswith("path[")
    assert len(hit["path_sha256_16"]) == 16


def test_worktree_symlink_name_and_target_are_scanned_without_leaking(tmp_path):
    secret_name = "AKIA" + "IOSFODNN7EXAMPLE"
    link = tmp_path / (secret_name + ".link")
    link.symlink_to("../" + secret_name)

    result = private_scan.scan_paths([tmp_path])
    dumped = json.dumps(result)

    assert secret_name not in dumped
    assert any(hit["rule"] == "aws_access_key" for hit in result["hits"])


def test_zip_directories_consume_budget_and_sensitive_name_is_redacted(tmp_path, monkeypatch):
    artifact = tmp_path / "directories.zip"
    secret_dir = "AKIA" + "IOSFODNN7EXAMPLE" + "/"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr(secret_dir, b"")
        archive.writestr("second/", b"")
    monkeypatch.setattr(private_scan, "MAX_ARCHIVE_MEMBERS", 1)

    result = private_scan.scan_paths([artifact])
    dumped = json.dumps(result)

    assert secret_dir not in dumped
    assert any(hit["rule"] == "aws_access_key" for hit in result["hits"])
    assert any(error["rule"] == "archive_member_limit_exceeded" for error in result["errors"])


@pytest.mark.parametrize(
    "member_type",
    [
        tarfile.DIRTYPE,
        tarfile.SYMTYPE,
        tarfile.LNKTYPE,
        tarfile.FIFOTYPE,
        tarfile.CHRTYPE,
        tarfile.BLKTYPE,
    ],
)
def test_tar_non_regular_members_are_budgeted_and_fail_closed_when_unsafe(
    tmp_path, member_type
):
    artifact = tmp_path / "metadata.tar"
    secret_name = "AKIA" + "IOSFODNN7EXAMPLE"
    with tarfile.open(artifact, "w") as archive:
        info = tarfile.TarInfo(secret_name)
        info.type = member_type
        if member_type in {tarfile.SYMTYPE, tarfile.LNKTYPE}:
            info.linkname = "../" + secret_name
        archive.addfile(info)

    result = private_scan.scan_paths([artifact])
    dumped = json.dumps(result)

    assert secret_name not in dumped
    assert any(hit["rule"] == "aws_access_key" for hit in result["hits"])
    if member_type != tarfile.DIRTYPE:
        assert any(
            error["rule"] == "archive_non_regular_member"
            for error in result["errors"]
        )


def test_zip_symlink_member_is_rejected_and_target_scanned(tmp_path):
    artifact = tmp_path / "symlink.zip"
    secret_target = "../" + "AKIA" + "IOSFODNN7EXAMPLE"
    info = zipfile.ZipInfo("link")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr(info, secret_target)

    result = private_scan.scan_paths([artifact])
    dumped = json.dumps(result)

    assert secret_target not in dumped
    assert any(hit["rule"] == "aws_access_key" for hit in result["hits"])
    assert any(error["rule"] == "archive_non_regular_member" for error in result["errors"])
