# Feishu Encrypted Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a real, opt-in Feishu Drive disaster-recovery path that packages a strictly verified credential-redacted export with an existing GPG public key, proves remote bytes by download, performs an isolated restore drill, and lets only that proof satisfy the cloud backup migration gate.

**Architecture:** Keep `feishu_drive_mirror.py` read-only and introduce `feishu_recovery.py` as a separate, explicit write-capable recovery subsystem. A recovery package contains a plaintext metadata-only manifest and GPG-encrypted split parts; upload occurs only after a terminal `--confirm-remote-write`, while remote verification downloads every uploaded part into a fresh directory and runs decrypt, strict export verification, and an ephemeral real restore. `export_restore.py` consumes only a private, hash-bound drill receipt and never treats a green upload or a locally fabricated state file as a cloud restore proof.

**Tech Stack:** Python 3.9–3.12 standard library, GnuPG, `lark-cli drive`, JSON, tar streaming, SHA-256, pytest, existing local Control Center.

---

## Safety Contract

- The live vault stays untouched. All tests use `tmp_path`; real commands require an explicit `--vault-dir` or an already-created export directory.
- Do not reuse `feishu_drive_mirror.py` for writes. It remains a read-only ingestion connector.
- Do not generate, import, export, print, or store a private key. `--recipient` accepts only an already-installed 40 or 64 hexadecimal public-key fingerprint.
- The Drive package must be built from a `backup --redact-secrets --fail-on-secrets` generation. A cloud copy intentionally restores memory with credential-shaped values redacted, exactly as the existing external-backup policy documents.
- The plaintext package manifest may contain only schema, timestamps, hash values, byte counts, redaction counts, package names, and a fingerprint suffix. It must contain no local paths, vault names, record text, credentials, open IDs, or Drive tokens.
- A Drive write requires both a parent folder token and `--confirm-remote-write`. The command creates one new package folder, uploads encrypted parts first and the package manifest last, and never overwrites an existing remote file.
- A migration accepts `external_cloud` only when the source export, remote exact-download check, decrypt-and-restore drill, current raw-index hash, timestamp freshness, health, and index parity all match. Upload success alone is always insufficient.
- The dashboard is read-only for cloud recovery. It may show proof state and the safe next action, but it must not contain an upload button or expose Drive tokens, full fingerprints, local paths, commands, or package internals.

## File Map

| File | Responsibility |
|---|---|
| `core/feishu_recovery.py` | Package schema, local GPG streaming, Lark Drive adapter, remote receipt, download verification, recovery drill, and command-line entrypoint. |
| `core/export_restore.py` | Read a private Feishu drill receipt, bind it to the current vault, and recognize `external_cloud` only through strict evidence. |
| `core/immortal.py` | Register the thin `feishu-recovery` command and choose portable or Feishu cloud evidence during migration preflight. |
| `core/control_data.py` | Publish a redacted cloud-recovery status to the System read model. |
| `core/product_assets/views/system.js` | Render a compact read-only cloud-recovery proof card inside System. |
| `docs/ARCHITECTURE.md` | Document the cloud recovery sequence and its non-equivalence to Drive sync. |
| `docs/PRIVACY.md` | Document encrypted redacted export, recipient-key ownership, and receipt privacy. |
| `README.md` | Provide explicit preparation, upload, verification, recovery, and migration-preflight commands. |
| `tests/test_feishu_recovery.py` | Package, confirmation, Drive, download, decrypt, receipt, and failure-boundary tests. |
| `tests/test_backup_migration_gate.py` | Cloud gate and current-source binding regressions. |
| `tests/test_control_data.py` | Redacted cloud status read model coverage. |
| `tests/test_product_ui_v2.py` | System card and no-upload-control contract. |

## Task 1: Define the safe package and receipt schemas

**Files:**
- Create: `core/feishu_recovery.py`
- Create: `tests/test_feishu_recovery.py`

- [x] **Step 1: Write failing source-export inspection tests**

```python
import hashlib
import json

import pytest

from feishu_recovery import RecoveryError, inspect_source_export


def write_export(export_dir, *, source_sha256="a" * 64, secret_count=2):
    export_dir.mkdir(parents=True)
    index = export_dir / "index.jsonl"
    index.write_text('{"id":"one","content":"[REDACTED_SECRET]"}\n', encoding="utf-8")
    receipt = export_dir / "secret-redaction-receipt.json"
    receipt.write_text(json.dumps({
        "mode": "jsonl-value-redaction-v1",
        "source_sha256": source_sha256,
        "export_sha256": hashlib.sha256(index.read_bytes()).hexdigest(),
        "unique_candidates": secret_count,
    }), encoding="utf-8")
    manifest = {
        "generated_at": "2026-07-22T00:00:00Z",
        "vault_dir": "/private/live/vault",
        "export_dir": str(export_dir),
        "secret_scan": {"unique_candidates": 0},
        "secret_redaction": {"receipt": "secret-redaction-receipt.json"},
        "warnings": [],
        "items": [
            {"relpath": "index.jsonl", "size": index.stat().st_size, "sha256": hashlib.sha256(index.read_bytes()).hexdigest()},
            {"relpath": "secret-redaction-receipt.json", "size": receipt.stat().st_size, "sha256": hashlib.sha256(receipt.read_bytes()).hexdigest()},
        ],
        "totals": {"files": 2, "bytes": index.stat().st_size + receipt.stat().st_size},
    }
    (export_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


def test_inspect_source_export_requires_strict_redaction_proof(tmp_path):
    export_dir = tmp_path / "immortal-export-safe"
    write_export(export_dir)

    inspected = inspect_source_export(export_dir)

    assert inspected["source_index_sha256"] == "a" * 64
    assert inspected["redaction_unique_candidates"] == 2
    assert "vault_dir" not in inspected
    assert "export_dir" not in inspected


def test_inspect_source_export_rejects_unredacted_or_non_strict_export(tmp_path):
    export_dir = tmp_path / "immortal-export-unsafe"
    manifest = write_export(export_dir)
    manifest["warnings"] = ["secret_shapes_present: 2"]
    manifest["secret_redaction"] = {}
    (export_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RecoveryError, match="source_export_not_strict"):
        inspect_source_export(export_dir)
```

- [x] **Step 2: Run the test to prove the module is missing**

Run:

```bash
PYTHONPATH=core python3 -m pytest tests/test_feishu_recovery.py -q
```

Expected: collection fails because `feishu_recovery` does not exist.

- [x] **Step 3: Implement metadata-only schema validation**

Add these stable constants and functions to `core/feishu_recovery.py`:

```python
PACKAGE_SCHEMA_VERSION = 1
RECEIPT_SCHEMA_VERSION = 1
PACKAGE_MANIFEST_NAME = "immortal-feishu-recovery.json"
PARTS_DIRNAME = "parts"
RECOVERY_DIRNAME = "recovery/feishu"
FINGERPRINT_RE = re.compile(r"\\A(?:[A-Fa-f0-9]{40}|[A-Fa-f0-9]{64})\\Z")


class RecoveryError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def require_fingerprint(value: str) -> str:
    candidate = str(value or "").strip().upper()
    if not FINGERPRINT_RE.fullmatch(candidate):
        raise RecoveryError("recipient_fingerprint_invalid")
    return candidate


def inspect_source_export(export_dir: str | Path) -> dict[str, Any]:
    check = export_restore.restore_check(export_dir, strict=True)
    if not check.get("ok"):
        raise RecoveryError("source_export_not_strict")
    manifest = read_json_object(Path(export_dir) / "manifest.json")
    redaction = manifest.get("secret_redaction")
    if not isinstance(redaction, dict) or redaction.get("receipt") != "secret-redaction-receipt.json":
        raise RecoveryError("source_export_redaction_missing")
    receipt = read_json_object(Path(export_dir) / "secret-redaction-receipt.json")
    if receipt.get("mode") != "jsonl-value-redaction-v1":
        raise RecoveryError("source_export_redaction_invalid")
    source_index_sha256 = receipt.get("source_sha256")
    if not isinstance(source_index_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", source_index_sha256):
        raise RecoveryError("source_export_source_hash_invalid")
    item_by_path = {str(item.get("relpath")): item for item in manifest.get("items", []) if isinstance(item, dict)}
    redacted_index = item_by_path.get("index.jsonl")
    receipt_item = item_by_path.get("secret-redaction-receipt.json")
    if (
        not isinstance(redacted_index, dict)
        or not isinstance(receipt_item, dict)
        or receipt.get("export_sha256") != redacted_index.get("sha256")
        or sha256_file(Path(export_dir) / "secret-redaction-receipt.json") != receipt_item.get("sha256")
    ):
        raise RecoveryError("source_export_redaction_binding_invalid")
    return {
        "generated_at": str(manifest.get("generated_at") or ""),
        "manifest_sha256": sha256_file(Path(export_dir) / "manifest.json"),
        "source_index_sha256": source_index_sha256,
        "redacted_index_sha256": str(receipt.get("export_sha256") or ""),
        "redaction_unique_candidates": int(receipt.get("unique_candidates") or 0),
        "files": int((manifest.get("totals") or {}).get("files") or 0),
        "bytes": int((manifest.get("totals") or {}).get("bytes") or 0),
        "content_fidelity": "credential_redacted",
    }
```

`read_json_object` must reject symlinks, non-regular files, malformed JSON, and bodies over 8 MiB. `inspect_source_export` must use only hashes and counts in its return value, never a path or an item relpath.

- [x] **Step 4: Add package-manifest validation tests and implementation**

```python
from feishu_recovery import build_package_manifest, validate_package_manifest


def test_package_manifest_has_no_local_path_or_drive_token():
    manifest = build_package_manifest(
        package_id="immortal-recovery-20260722-a1b2c3d4",
        recipient_fingerprint="A" * 40,
        source={
            "generated_at": "2026-07-22T00:00:00Z",
            "manifest_sha256": "b" * 64,
            "source_index_sha256": "c" * 64,
            "redacted_index_sha256": "d" * 64,
            "redaction_unique_candidates": 2,
            "files": 2,
            "bytes": 12,
            "content_fidelity": "credential_redacted",
        },
        parts=[{"name": "parts/part-00001.gpg", "bytes": 12, "sha256": "e" * 64}],
    )

    validate_package_manifest(manifest)
    encoded = json.dumps(manifest, sort_keys=True)
    assert "/private" not in encoded
    assert "folder_token" not in encoded
    assert manifest["recipient_fingerprint_suffix"] == "AAAAAAAA"
```

Implement `build_package_manifest` so it stores `recipient_fingerprint_suffix`, not the full fingerprint. Implement `validate_package_manifest` so it rejects unknown top-level keys, invalid part names, duplicate parts, non-contiguous part numbers, non-SHA256 values, a non-redacted source descriptor, or a path-like value in any scalar string.

- [x] **Step 5: Run Task 1 tests and commit**

Run:

```bash
PYTHONPATH=core python3 -m pytest tests/test_feishu_recovery.py -q
git add core/feishu_recovery.py tests/test_feishu_recovery.py
git commit -m "feat: define Feishu recovery package evidence"
```

Expected: all Task 1 tests pass.

## Task 2: Stream an encrypted split package without a plaintext cloud artifact

**Files:**
- Modify: `core/feishu_recovery.py`
- Modify: `tests/test_feishu_recovery.py`

- [ ] **Step 1: Add failing package-build tests using a fake cryptor**

```python
from io import BytesIO
import tarfile

from feishu_recovery import build_encrypted_package, verify_local_package


class CopyCryptor:
    def assert_recipient(self, fingerprint):
        assert fingerprint == "A" * 40

    def encrypt(self, fingerprint, write_plaintext, write_ciphertext):
        plain = BytesIO()
        write_plaintext(plain)
        write_ciphertext(plain.getvalue())


def test_build_encrypted_package_splits_and_hashes_all_ciphertext(tmp_path):
    export_dir = tmp_path / "immortal-export-safe"
    write_export(export_dir)
    package_dir = tmp_path / "package"

    result = build_encrypted_package(
        export_dir,
        package_dir,
        recipient_fingerprint="A" * 40,
        part_bytes=17,
        cryptor=CopyCryptor(),
    )

    assert result["ok"] is True
    assert len(result["parts"]) > 1
    assert verify_local_package(package_dir)["ok"] is True
    assert not (package_dir / "export").exists()


def test_build_encrypted_package_cleans_partial_parts_after_cryptor_failure(tmp_path):
    class FailingCryptor(CopyCryptor):
        def encrypt(self, fingerprint, write_plaintext, write_ciphertext):
            write_ciphertext(b"partial")
            raise RuntimeError("injected")

    export_dir = tmp_path / "immortal-export-safe"
    write_export(export_dir)

    result = build_encrypted_package(export_dir, tmp_path / "package", "A" * 40, cryptor=FailingCryptor())

    assert result["ok"] is False
    assert result["blockers"] == ["encryption_failed"]
    assert not (tmp_path / "package").exists()
```

- [ ] **Step 2: Run the targeted test and confirm it fails**

Run:

```bash
PYTHONPATH=core python3 -m pytest tests/test_feishu_recovery.py -q
```

Expected: `build_encrypted_package` and `verify_local_package` are missing.

- [ ] **Step 3: Implement a bounded split writer and production GPG adapter**

Implement the following interfaces exactly:

```python
class SplitDigestWriter:
    def __init__(self, parts_dir: Path, part_bytes: int) -> None:
        if part_bytes < 1024 * 1024:
            raise RecoveryError("part_size_too_small")
        self.parts_dir = parts_dir
        self.part_bytes = part_bytes
        self._part_number = 0
        self._current: BinaryIO | None = None
        self._current_bytes = 0
        self._current_digest: Any = None
        self._parts: list[dict[str, Any]] = []

    def _open_part(self) -> None:
        self._part_number += 1
        self.parts_dir.mkdir(parents=True, exist_ok=True)
        self._current_path = self.parts_dir / f"part-{self._part_number:05d}.gpg"
        self._current = self._current_path.open("xb")
        self._current_bytes = 0
        self._current_digest = hashlib.sha256()

    def _finish_part(self) -> None:
        if self._current is None:
            return
        self._current.flush()
        os.fsync(self._current.fileno())
        self._current.close()
        self._parts.append({
            "name": f"parts/{self._current_path.name}",
            "bytes": self._current_bytes,
            "sha256": self._current_digest.hexdigest(),
        })
        self._current = None
        self._current_digest = None

    def write(self, data: bytes) -> int:
        remaining = memoryview(data)
        while remaining:
            if self._current is None:
                self._open_part()
            room = self.part_bytes - self._current_bytes
            chunk = remaining[:room]
            self._current.write(chunk)
            self._current_digest.update(chunk)
            self._current_bytes += len(chunk)
            remaining = remaining[len(chunk):]
            if self._current_bytes == self.part_bytes:
                self._finish_part()
        return len(data)

    def close(self) -> list[dict[str, Any]]:
        self._finish_part()
        return list(self._parts)


class OpenPGPCryptor:
    def _pump_gpg(
        self,
        argv: Sequence[str],
        write_stdin: Callable[[BinaryIO], None],
        consume_stdout: Callable[[BinaryIO], None],
    ) -> None:
        process = subprocess.Popen(
            list(argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        writer_error: list[BaseException] = []

        def write_input() -> None:
            try:
                assert process.stdin is not None
                write_stdin(process.stdin)
            except BaseException as exc:
                writer_error.append(exc)
            finally:
                if process.stdin is not None:
                    process.stdin.close()

        writer = threading.Thread(target=write_input, daemon=True)
        writer.start()
        try:
            assert process.stdout is not None
            consume_stdout(process.stdout)
        finally:
            if process.stdout is not None:
                process.stdout.close()
        writer.join()
        stderr = process.stderr.read() if process.stderr is not None else b""
        returncode = process.wait()
        if writer_error or returncode != 0:
            raise RecoveryError("gpg_stream_failed")

    def assert_recipient(self, fingerprint: str) -> None:
        completed = subprocess.run(
            ["gpg", "--batch", "--with-colons", "--list-keys", fingerprint],
            capture_output=True, text=True, check=False, timeout=30,
        )
        rows = [line.split(":") for line in completed.stdout.splitlines()]
        if completed.returncode != 0 or not any(row[:1] == ["fpr"] and row[9].upper() == fingerprint for row in rows if len(row) > 9):
            raise RecoveryError("recipient_public_key_unavailable")

    def encrypt(
        self,
        fingerprint: str,
        write_plaintext: Callable[[BinaryIO], None],
        write_ciphertext: Callable[[bytes], int],
    ) -> None:
        self._pump_gpg(
            ["gpg", "--batch", "--yes", "--trust-model", "always", "--encrypt", "--recipient", fingerprint, "--output", "-"],
            write_plaintext, write_ciphertext,
        )

    def decrypt(
        self,
        part_paths: Sequence[Path],
        consume_plaintext: Callable[[BinaryIO], None],
    ) -> None:
        def write_ciphertext(stream: BinaryIO) -> None:
            for part in part_paths:
                with part.open("rb") as handle:
                    shutil.copyfileobj(handle, stream, length=1024 * 1024)
        self._pump_gpg(["gpg", "--batch", "--yes", "--decrypt", "--output", "-"], write_ciphertext, consume_plaintext)
```

`OpenPGPCryptor.assert_recipient` runs `gpg --batch --with-colons --list-keys <fingerprint>` and requires an exact primary `fpr` record. `encrypt` starts `gpg --batch --yes --trust-model always --encrypt --recipient <fingerprint> --output -`, streams a tar writer through stdin on a writer thread, and writes stdout into `SplitDigestWriter`. `decrypt` concatenates parts into `gpg --batch --yes --decrypt --output -`, then passes stdout to a tar consumer. It must return a stable failure code without including GPG stderr in user-visible results.

Use `tarfile.open(fileobj=stream, mode="w|")` and write this exact archive order:

```text
export/manifest.json
export/<each manifest item relpath in sorted order>
```

Reject non-regular files, symlinks, duplicate archive names, missing item bodies, and an export-generation change between preflight and postflight. The package directory is created with `0o700`; manifest and local receipts use `0o600`; failure removes the entire package directory.

- [ ] **Step 4: Implement local package verification**

`verify_local_package(package_dir)` must reject a package whose directory contains an unlisted file, symlink, missing part, size mismatch, SHA mismatch, invalid manifest, or part count mismatch. Its return value contains only `ok`, `package_id`, `parts`, `bytes`, and blocker codes.

- [ ] **Step 5: Run Task 2 tests and commit**

Run:

```bash
PYTHONPATH=core python3 -m pytest tests/test_feishu_recovery.py -q
git add core/feishu_recovery.py tests/test_feishu_recovery.py
git commit -m "feat: build encrypted Feishu recovery packages"
```

Expected: package tests pass without contacting GPG or Feishu in the test suite.

## Task 3: Add an explicit Drive write adapter and upload receipt

**Files:**
- Modify: `core/feishu_recovery.py`
- Modify: `tests/test_feishu_recovery.py`

- [ ] **Step 1: Write failing confirmation and command-shape tests**

```python
from feishu_recovery import LarkDriveClient, upload_package


class RecordingRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, *, cwd=None):
        self.calls.append((argv, cwd))
        if "+create-folder" in argv:
            return {"data": {"token": "folder_new_12345678"}}
        if "+status" in argv:
            return {"ok": True, "detection": "exact"}
        return {"data": {"file_token": "file_12345678"}}


def test_upload_requires_explicit_remote_write_confirmation(tmp_path):
    package = make_valid_package(tmp_path)
    runner = RecordingRunner()

    result = upload_package(package, "parent_12345678", client=LarkDriveClient(runner), confirm_remote_write=False)

    assert result["ok"] is False
    assert result["blockers"] == ["remote_write_confirmation_required"]
    assert runner.calls == []


def test_upload_creates_dedicated_folder_uploads_manifest_last_and_runs_exact_status(tmp_path):
    package = make_valid_package(tmp_path)
    runner = RecordingRunner()

    result = upload_package(package, "parent_12345678", client=LarkDriveClient(runner), confirm_remote_write=True)

    upload_calls = [argv for argv, _cwd in runner.calls if "+upload" in argv]
    assert result["ok"] is True
    assert upload_calls[-1][upload_calls[-1].index("--name") + 1] == "immortal-feishu-recovery.json"
    assert any("+status" in argv for argv, _cwd in runner.calls)
    assert "parent_12345678" not in json.dumps(result["receipt"])
```

- [ ] **Step 2: Run tests to confirm the upload path is absent**

Run:

```bash
PYTHONPATH=core python3 -m pytest tests/test_feishu_recovery.py -q
```

Expected: `LarkDriveClient` and `upload_package` are missing.

- [ ] **Step 3: Implement the adapter and receipt persistence**

Implement `LarkDriveClient` around an injectable runner. Production execution uses these exact command forms:

```text
lark-cli drive +create-folder --as user --folder-token <parent> --name <package-id> --format json
lark-cli drive +upload --as user --folder-token <new-folder> --file <part> --name <part-name> --format json
lark-cli drive +upload --as user --folder-token <new-folder> --file <manifest> --name immortal-feishu-recovery.json --format json
lark-cli drive +status --as user --folder-token <new-folder> --local-dir <package-name> --format json
```

`upload_package` must:

1. call `verify_local_package` before any write;
2. reject a missing confirmation or invalid opaque token before any runner call;
3. create a new folder under the supplied parent, not Drive root;
4. upload every encrypted part in manifest order, then upload the metadata manifest last;
5. require `detection == "exact"` and `ok is True` from `+status`;
6. write a `remote-upload-receipt.json` with package manifest SHA, part names and hashes, remote file tokens, a hashed remote folder identifier, and status mode;
7. never put a full folder token, a local path, command line, stderr, or raw response in the receipt returned to the dashboard.

Write the private receipt under `<vault>/recovery/feishu/receipts/<package-id>.json` and atomically update `<vault>/recovery/feishu/latest-upload.json`. Both files are mode `0o600` and are ignored by Git because they live in the vault.

- [ ] **Step 4: Add failure tests**

Add parameterized tests for malformed create-folder responses, missing file token, non-exact status, a failed part upload, and an injected receipt write error. Assert that the returned blocker is stable and no success receipt is persisted.

- [ ] **Step 5: Run Task 3 tests and commit**

Run:

```bash
PYTHONPATH=core python3 -m pytest tests/test_feishu_recovery.py -q
git add core/feishu_recovery.py tests/test_feishu_recovery.py
git commit -m "feat: upload encrypted recovery package to Feishu Drive"
```

Expected: all write behavior is confirmation-gated and all tests remain offline.

## Task 4: Prove recovery by fresh remote download, decrypt, and isolated restore

**Files:**
- Modify: `core/feishu_recovery.py`
- Modify: `tests/test_feishu_recovery.py`

- [ ] **Step 1: Write failing recovery-drill tests**

```python
from feishu_recovery import run_recovery_drill


class CopyCryptorWithDecrypt(CopyCryptor):
    def decrypt(self, part_paths, consume_plaintext):
        consume_plaintext(BytesIO(b"".join(path.read_bytes() for path in part_paths)))


def test_recovery_drill_downloads_all_parts_and_runs_real_restore(tmp_path):
    package = make_valid_package(tmp_path, cryptor=CopyCryptorWithDecrypt())
    receipt = make_remote_receipt(package)
    client = DownloadingClient.from_package(package)

    result = run_recovery_drill(receipt, tmp_path / "drill", client=client, cryptor=CopyCryptorWithDecrypt())

    assert result["ok"] is True
    assert result["verification_mode"] == "remote-download-sha256+decrypt-restore"
    assert not (tmp_path / "drill" / "recovered").exists()
    assert result["source_index_sha256"] == "a" * 64


def test_recovery_drill_fails_when_downloaded_part_hash_changes(tmp_path):
    package = make_valid_package(tmp_path, cryptor=CopyCryptorWithDecrypt())
    receipt = make_remote_receipt(package)
    client = DownloadingClient.from_package(package, corrupt_name="parts/part-00001.gpg")

    result = run_recovery_drill(receipt, tmp_path / "drill", client=client, cryptor=CopyCryptorWithDecrypt())

    assert result["ok"] is False
    assert result["blockers"] == ["remote_part_hash_mismatch"]
```

- [ ] **Step 2: Run the targeted tests and confirm red**

Run:

```bash
PYTHONPATH=core python3 -m pytest tests/test_feishu_recovery.py -q
```

Expected: `run_recovery_drill` is missing.

- [ ] **Step 3: Implement download, safe tar extraction, and isolated restore**

Implement `run_recovery_drill(upload_receipt, drill_root, *, client, cryptor)` with this exact sequence:

```text
create fresh 0700 drill directory
download manifest and every receipt-listed remote part to download/package
verify local package schema, part sizes, and SHA-256 values
stream GPG decrypt into a tar reader
require export/manifest.json as first regular file
allow only expected regular export files, reject symlink, hardlink, device, duplicate, absolute, and traversal members
run restore_check(extracted/export, strict=True)
run restore_export(extracted/export, recovered-vault, rebind_vault_config=True)
delete extracted and recovered payloads in finally
write only a private hash-and-count recovery drill receipt
```

The success receipt is `<vault>/recovery/feishu/receipts/<package-id>.drill.json`, and `latest-drill.json` points to it. It contains `schema_version`, `provider`, `verified_at`, `package_id`, `package_manifest_sha256`, `source_index_sha256`, `source_export_generated_at`, `verification_mode`, `remote_parts`, `restore_check`, and `recovery_drill`. It must not contain paths, tokens, ciphertext, manifest bodies, or source text.

- [ ] **Step 4: Add negative extraction and proof-binding tests**

Add tests that reject a tar symlink, archive path traversal, a manifest not first, duplicate archive member, mismatched source-export manifest SHA, absent private-key decrypt result, and a receipt whose source index hash differs from a later vault hash.

- [ ] **Step 5: Run Task 4 tests and commit**

Run:

```bash
PYTHONPATH=core python3 -m pytest tests/test_feishu_recovery.py tests/test_export_restore.py -q
git add core/feishu_recovery.py tests/test_feishu_recovery.py
git commit -m "feat: verify Feishu recovery through restore drill"
```

Expected: no test needs a real Drive account or a GPG private key.

## Task 5: Wire cloud proof into the migration gate and CLI

**Files:**
- Modify: `core/export_restore.py`
- Modify: `core/immortal.py`
- Modify: `tests/test_backup_migration_gate.py`
- Modify: `tests/test_feishu_recovery.py`

- [ ] **Step 1: Write failing cloud-gate tests**

```python
def valid_cloud_evidence():
    return {
        "generated_at": "2026-07-20T03:30:00Z",
        "storage": {"location": "external_cloud", "provider": "feishu_drive"},
        "verification": {"mode": "remote-download-sha256+decrypt-restore", "ok": True},
        "restore_check": {"ok": True, "strict": True},
        "recovery_drill": {"ok": True, "mode": "decrypt-restore"},
        "source_binding": {"ok": True},
        "secret_scan": {"unique_candidates": 0},
        "warnings": [],
        "health": {"ok": True},
        "index_parity": {"ok": True},
    }


def test_verified_feishu_cloud_evidence_passes_external_gate():
    assert export_restore.migration_backup_gate(valid_cloud_evidence(), now=NOW)["ok"] is True


def test_cloud_upload_without_download_restore_or_source_binding_blocks():
    evidence = valid_cloud_evidence()
    evidence.pop("recovery_drill")
    evidence["source_binding"] = {"ok": False}

    result = export_restore.migration_backup_gate(evidence, now=NOW)

    assert {"cloud_recovery_drill_missing_or_failed", "cloud_source_binding_failed"}.issubset(result["blockers"])
```

- [ ] **Step 2: Run the test to show current gate rejects cloud evidence**

Run:

```bash
PYTHONPATH=core python3 -m pytest tests/test_backup_migration_gate.py -q
```

Expected: cloud evidence receives `backup_not_external`.

- [ ] **Step 3: Implement fail-closed cloud evidence selection**

Add `get_feishu_recovery_backup_status(vault_dir)` to `core/export_restore.py`. It imports `feishu_recovery` lazily, reads `latest-drill.json` with the existing no-follow private JSON helpers, validates schema and receipt binding, compares `source_index_sha256` with `sha256_file(vault / "index.jsonl")`, and returns only safe evidence fields.

Extend `migration_backup_gate` as follows:

```python
cloud = location == "external_cloud"
if require_external and location not in {"external_disk", "external_cloud"}:
    blockers.append("backup_not_external")
if cloud and payload.get("provider") != "feishu_drive":
    blockers.append("cloud_provider_invalid")
if cloud and verification_mode != "remote-download-sha256+decrypt-restore":
    blockers.append("cloud_verification_not_remote_strict")
drill = payload.get("recovery_drill")
source_binding = payload.get("source_binding")
if cloud and (not isinstance(drill, dict) or drill.get("ok") is not True):
    blockers.append("cloud_recovery_drill_missing_or_failed")
if cloud and (not isinstance(source_binding, dict) or source_binding.get("ok") is not True):
    blockers.append("cloud_source_binding_failed")
```

Preserve all existing `external_disk` behavior and tests. Do not relax secret-scan checks: cloud packages are built from the existing credential-redacted export policy.

- [ ] **Step 4: Register the explicit CLI without widening old aliases**

Add these parser shapes in `core/immortal.py`:

```python
feishu_recovery = sub.add_parser(
    "feishu-recovery",
    help="Prepare, upload, verify, or restore an encrypted Feishu disaster-recovery package",
)
feishu_recovery.add_argument("feishu_recovery_args", nargs=argparse.REMAINDER)
feishu_recovery.set_defaults(func=command_feishu_recovery)

migration_preflight.add_argument(
    "--backup-source",
    choices=("portable", "feishu-cloud"),
    default="portable",
)
```

`command_feishu_recovery` forwards only its remainder to `feishu_recovery.py`. `command_migration_preflight` calls `get_feishu_recovery_backup_status` only when `backup_source == "feishu-cloud"`, preserving existing tests that pass an older `argparse.Namespace` by using `getattr(args, "backup_source", "portable")`.

- [ ] **Step 5: Run migration and CLI tests, then commit**

Run:

```bash
PYTHONPATH=core python3 -m pytest \
  tests/test_feishu_recovery.py \
  tests/test_backup_migration_gate.py \
  tests/test_export_restore.py -q
git add core/export_restore.py core/immortal.py tests/test_backup_migration_gate.py tests/test_feishu_recovery.py
git commit -m "feat: gate migration on verified Feishu recovery"
```

Expected: an absent, stale, malformed, locally altered, or upload-only cloud receipt fails closed.

## Task 6: Show the real cloud recovery state in System without an unsafe action

**Files:**
- Modify: `core/control_data.py`
- Modify: `core/product_assets/views/system.js`
- Modify: `tests/test_control_data.py`
- Modify: `tests/test_product_ui_v2.py`

- [ ] **Step 1: Add failing read-model tests**

```python
def test_backups_exposes_redacted_cloud_recovery_state(tmp_path, monkeypatch):
    data = ControlData(tmp_path, skill_dir=tmp_path)
    monkeypatch.setattr(
        export_restore,
        "get_feishu_recovery_backup_status",
        lambda _vault: {
            "ok": True,
            "storage_location": "external_cloud",
            "provider": "feishu_drive",
            "generated_at": "2026-07-22T00:00:00Z",
            "verification_mode": "remote-download-sha256+decrypt-restore",
            "recovery_drill": {"ok": True},
            "source_binding": {"ok": True},
        },
    )

    cloud = data.backups()["cloud_recovery"]

    assert cloud["status"] == "verified"
    assert "folder_token" not in cloud
    assert "path" not in json.dumps(cloud)
```

- [ ] **Step 2: Run tests and confirm the field is missing**

Run:

```bash
PYTHONPATH=core python3 -m pytest tests/test_control_data.py tests/test_product_ui_v2.py -q
```

Expected: `cloud_recovery` is absent and the System view has no recovery card.

- [ ] **Step 3: Implement safe System evidence**

`ControlData.backups()` adds this bounded object:

```python
"cloud_recovery": {
    "status": "verified" | "attention" | "unknown",
    "provider": "Feishu Drive" | "",
    "last_verified_at": "<ISO timestamp or empty>",
    "verification": "remote-download-sha256+decrypt-restore" | "",
    "source_binding": "matched" | "missing" | "mismatch",
    "reason_code": "<stable code only>",
    "action": "终端执行加密上传与恢复演练" | "无需操作",
}
```

The implementation must not return receipt paths, part names, remote folder identifiers, remote file tokens, recipient fingerprints, commands, or raw failure text. `system.js` renders a dedicated card titled `飞书异地恢复` with one of these exact human states: `未配置`, `等待恢复演练`, `已验证可恢复`, `证据失效`. It must show a clear next action but no `button`, form, route, or endpoint for upload.

- [ ] **Step 4: Add UI contract tests**

```python
def test_system_ui_shows_cloud_recovery_evidence_without_upload_action():
    page = (ASSET_ROOT / "views" / "system.js").read_text(encoding="utf-8")
    assert "飞书异地恢复" in page
    assert "已验证可恢复" in page
    assert "confirm-remote-write" not in page
    assert "feishu-recovery upload" not in page
```

- [ ] **Step 5: Run Task 6 tests and commit**

Run:

```bash
PYTHONPATH=core python3 -m pytest tests/test_control_data.py tests/test_product_ui_v2.py -q
git add core/control_data.py core/product_assets/views/system.js tests/test_control_data.py tests/test_product_ui_v2.py
git commit -m "feat: surface verified cloud recovery evidence"
```

Expected: the dashboard is informative but never becomes a one-click cloud-write surface.

## Task 7: Document, package, and verify the public-private boundary

**Files:**
- Modify: `README.md`
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/PRIVACY.md`
- Modify: `tests/test_v11_packaging.py`

- [ ] **Step 1: Add exact operator documentation**

Add this staged command sequence to `README.md`, replacing placeholders only with user-owned paths and an already-installed public-key fingerprint:

```bash
immortal-memory backup \
  --vault-dir "$HOME/.immortal" \
  --output-dir "$HOME/.immortal/recovery/exports" \
  --redact-secrets --fail-on-secrets

immortal-memory feishu-recovery prepare \
  --export-dir "$HOME/.immortal/recovery/exports/immortal-export-<timestamp>" \
  --package-dir "$HOME/.immortal/recovery/packages/<package-id>" \
  --recipient <PUBLIC_KEY_FINGERPRINT>

immortal-memory feishu-recovery upload \
  --package-dir "$HOME/.immortal/recovery/packages/<package-id>" \
  --parent-folder-token <USER_OWNED_FOLDER_TOKEN> \
  --confirm-remote-write

immortal-memory feishu-recovery drill \
  --receipt "$HOME/.immortal/recovery/feishu/receipts/<package-id>.json"

immortal-memory migration-preflight \
  --require-external-backup \
  --backup-source feishu-cloud \
  --json
```

Explain that `prepare` does not upload, `upload` writes private encrypted data, `drill` requires the matching private key and proves restoration, and no command should target Drive root or a shared untrusted folder.

- [ ] **Step 2: Update architecture and privacy contracts**

Add the exact state sequence `local verified redacted export -> GPG encrypted parts -> confirmed Feishu upload -> exact remote download -> decrypt and isolated restore -> private proof receipt -> migration gate`. State explicitly that Drive synchronization, a successful upload, a local receipt, and a green dashboard card without a current drill are not disaster-recovery proof. State that GitHub release scanning and encrypted private recovery are separate pipelines.

- [ ] **Step 3: Add packaging closure test**

```python
def test_wheel_contains_feishu_recovery_module_and_no_vault_artifacts(tmp_path):
    wheel = build_wheel(tmp_path)
    names = wheel_names(wheel)
    assert "immortal_memory/feishu_recovery.py" in names
    assert not any(".immortal" in name or "recovery/feishu" in name for name in names)
```

- [ ] **Step 4: Run the full verification set**

Run:

```bash
PYTHONPATH=core python3 -m compileall -q core
PYTHONPATH=core python3 -m pytest -q
python3 -m build
git ls-files -z | xargs -0 python3 scripts/private_scan.py
git diff --check
```

Expected: all tests pass, source and wheel contain no private vault content, and the package version remains the intended `1.1.0` release version already synchronized in `core/VERSION` and `pyproject.toml`.

- [ ] **Step 5: Browser acceptance against an isolated vault**

Run the System view against a temporary vault with a synthetic verified cloud receipt and confirm:

```text
the card says 已验证可恢复
the card shows no token, path, fingerprint, command, or upload button
the existing isolated-vault action-disablement remains intact
```

Capture only synthetic test data in `output/playwright/`; do not capture a live vault or commit screenshots.

- [ ] **Step 6: Commit documentation and verification changes**

```bash
git add README.md docs/ARCHITECTURE.md docs/PRIVACY.md tests/test_v11_packaging.py
git commit -m "docs: document Feishu encrypted recovery workflow"
```

## Plan Self-Review

- Spec coverage: package preparation is Task 1–2; confirmation-gated remote write is Task 3; real remote restore proof is Task 4; migration gating is Task 5; user-visible truthful status is Task 6; documentation and release hygiene are Task 7.
- Privacy: source data stays encrypted in Drive; only a credential-redacted export is packaged; plaintext metadata contains no local path or token; dashboard receives a bounded status only.
- Failure handling: no public key, invalid source export, upload-only success, bad remote bytes, failed decrypt, unsafe archive member, stale receipt, or changed live index all fail closed.
- Type consistency: `external_cloud`, `feishu_drive`, `remote-download-sha256+decrypt-restore`, `source_binding`, and `recovery_drill` are used consistently in package, receipt, gate, dashboard, and docs.
