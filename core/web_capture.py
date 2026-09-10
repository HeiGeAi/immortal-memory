#!/usr/bin/env python3
"""Hybrid web capture for the Immortal vault.

Browser history is captured as metadata only. Full page text is saved only for
manual saves or domains explicitly allowlisted in the local config.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import shutil
import sqlite3
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from config import configured_vault_dir, load_config
from index_writer import append_jsonl_records
from maintenance_gate import writer_access


UTC = timezone.utc
CHROME_EPOCH = datetime(1601, 1, 1, tzinfo=UTC)
DEFAULT_HISTORY_LOOKBACK_HOURS = 24
DEFAULT_MAX_VISITS_PER_RUN = 200
DEFAULT_MAX_PAGES_PER_RUN = 5
DEFAULT_DENY_DOMAINS = [
    "accounts.google.com",
    "login.microsoftonline.com",
    "login.live.com",
    "appleid.apple.com",
    "icloud.com",
    "gmail.com",
    "mail.google.com",
    "outlook.live.com",
    "outlook.office.com",
    "paypal.com",
    "stripe.com",
    "alipay.com",
    "weixin.qq.com",
    "web.wechat.com",
]
DENY_DOMAIN_KEYWORDS = {
    "bank",
    "banking",
    "pay",
    "payment",
    "wallet",
    "passport",
    "signin",
    "login",
    "auth",
    "account",
    "password",
    "mail",
    "gmail",
    "outlook",
    "medical",
    "health",
    "hospital",
    "insurance",
}
INTERNAL_SCHEMES = {"about", "chrome", "edge", "brave", "file", "data", "javascript", "view-source"}


class TextExtractor(HTMLParser):
    """Small stdlib HTML text extractor for saved page snapshots."""

    BLOCK_TAGS = {"article", "main", "section", "p", "li", "h1", "h2", "h3", "h4", "pre", "blockquote"}
    SKIP_TAGS = {"script", "style", "noscript", "svg", "form", "nav", "header", "footer"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self._in_title = False
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "title":
            self._in_title = True
            return
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag in self.BLOCK_TAGS:
            self.text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
            return
        if tag in self.SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag in self.BLOCK_TAGS:
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
            return
        if self._skip_depth:
            return
        if data.strip():
            self.text_parts.append(data)

    def title(self) -> str:
        return normalize_space(" ".join(self.title_parts))

    def text(self) -> str:
        text = "".join(self.text_parts)
        text = html.unescape(text)
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\n\s+", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def chrome_time_to_dt(value: int | float | None) -> datetime | None:
    try:
        raw = int(value or 0)
    except (TypeError, ValueError):
        return None
    if raw <= 0:
        return None
    return CHROME_EPOCH + timedelta(microseconds=raw)


def dt_to_chrome_time(value: datetime) -> int:
    return int((value.astimezone(UTC) - CHROME_EPOCH).total_seconds() * 1_000_000)


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def safe_hash(value: str, length: int = 16) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()[:length]


def normalize_domain(domain: str) -> str:
    domain = domain.lower().strip().strip(".")
    if domain.startswith("www."):
        domain = domain[4:]
    return domain


def domain_matches(domain: str, patterns: list[str]) -> bool:
    domain = normalize_domain(domain)
    if not domain:
        return False
    for item in patterns:
        pattern = normalize_domain(str(item or ""))
        if not pattern:
            continue
        if pattern.startswith("*."):
            pattern = pattern[2:]
        if domain == pattern or domain.endswith(f".{pattern}"):
            return True
    return False


def is_sensitive_domain(domain: str, deny_domains: list[str]) -> bool:
    normalized = normalize_domain(domain)
    if not normalized:
        return True
    if domain_matches(normalized, deny_domains):
        return True
    labels = re.split(r"[\.-]+", normalized)
    return any(label in DENY_DOMAIN_KEYWORDS for label in labels)


def sanitize_url(raw_url: str, *, strip_query: bool = True) -> tuple[str, str, bool]:
    parsed = urllib.parse.urlsplit(str(raw_url or "").strip())
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    domain = normalize_domain(hostname)
    authority = f"[{hostname}]" if ":" in hostname else hostname
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is not None:
        authority = f"{authority}:{port}"
    query_redacted = bool(parsed.query or parsed.fragment)
    if strip_query:
        query = ""
    else:
        pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        query = urllib.parse.urlencode([(key, "[redacted]") for key, _ in pairs[:20]])
    sanitized = urllib.parse.urlunsplit((scheme, authority, parsed.path or "/", query, ""))
    return sanitized, domain, query_redacted


def browser_profiles(config: dict[str, Any]) -> list[dict[str, str]]:
    web_config = config.get("web_capture") if isinstance(config.get("web_capture"), dict) else {}
    raw = web_config.get("browsers") or []
    result: list[dict[str, str]] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                name = item.lower()
                if name == "chrome":
                    result.append({
                        "name": "chrome",
                        "history_path": "~/Library/Application Support/Google/Chrome/Default/History",
                    })
                elif name == "edge":
                    result.append({
                        "name": "edge",
                        "history_path": "~/Library/Application Support/Microsoft Edge/Default/History",
                    })
                continue
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("browser") or "browser").strip() or "browser"
            history_path = str(item.get("history_path") or "").strip()
            profile = str(item.get("profile") or "Default").strip() or "Default"
            if not history_path and name.lower() == "chrome":
                history_path = f"~/Library/Application Support/Google/Chrome/{profile}/History"
            if not history_path and name.lower() == "edge":
                history_path = f"~/Library/Application Support/Microsoft Edge/{profile}/History"
            if history_path:
                result.append({"name": name, "history_path": history_path})
    return result


def web_paths(vault_dir: Path) -> dict[str, Path]:
    web_dir = vault_dir / "web"
    return {
        "web_dir": web_dir,
        "pages_dir": web_dir / "pages",
        "state": web_dir / "state.json",
        "latest": web_dir / "latest.json",
        "web_index": web_dir / "index.jsonl",
        "daily_dir": vault_dir / "daily",
        "index": vault_dir / "index.jsonl",
    }


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def load_existing_dedup(vault_dir: Path, source: str) -> set[str]:
    index_file = vault_dir / "index.jsonl"
    seen: set[str] = set()
    if not index_file.exists():
        return seen
    with index_file.open("r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("source") != source:
                continue
            dedup_key = record.get("_dedup_key") or (record.get("metadata") or {}).get("dedup_key")
            if dedup_key:
                seen.add(str(dedup_key))
    return seen


def count_source_records(vault_dir: Path, source: str) -> int:
    index_file = vault_dir / "index.jsonl"
    if not index_file.exists():
        return 0
    total = 0
    with index_file.open("r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("source") == source:
                total += 1
    return total


def _append_records_locked(vault_dir: Path, records: list[dict[str, Any]]) -> None:
    if not records:
        return
    paths = web_paths(vault_dir)
    paths["daily_dir"].mkdir(parents=True, exist_ok=True)
    paths["index"].parent.mkdir(parents=True, exist_ok=True)
    by_date: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        date = str(record.get("timestamp") or now_iso())[:10]
        by_date.setdefault(date, []).append(record)

    for date, items in sorted(by_date.items()):
        with (paths["daily_dir"] / f"{date}.jsonl").open("a", encoding="utf-8") as handle:
            for record in items:
                clean = {k: v for k, v in record.items() if not k.startswith("_")}
                handle.write(json.dumps(clean, ensure_ascii=False) + "\n")
    append_jsonl_records(
        paths["index"],
        records,
        maintenance_held=True,
    )


def append_records(vault_dir: Path, records: list[dict[str, Any]]) -> None:
    if not records:
        return
    with writer_access(vault_dir):
        _append_records_locked(vault_dir, records)


def copy_history_db(history_path: Path) -> Path:
    tmp_dir = Path(tempfile.mkdtemp(prefix="immortal-web-history-"))
    tmp_path = tmp_dir / "History.sqlite3"
    shutil.copy2(history_path, tmp_path)
    return tmp_path


def read_history_rows(history_path: Path, *, since_dt: datetime, limit: int) -> list[dict[str, Any]]:
    if not history_path.exists():
        raise FileNotFoundError(str(history_path))
    copied = copy_history_db(history_path)
    try:
        conn = sqlite3.connect(f"file:{copied}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        threshold = dt_to_chrome_time(since_dt)
        rows = conn.execute(
            """
            SELECT url, title, visit_count, typed_count, last_visit_time
            FROM urls
            WHERE last_visit_time >= ?
            ORDER BY last_visit_time DESC
            LIMIT ?
            """,
            (threshold, limit),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        try:
            conn.close()  # type: ignore[name-defined]
        except Exception:
            pass
        shutil.rmtree(copied.parent, ignore_errors=True)


def should_skip_url(url: str, domain: str, deny_domains: list[str]) -> tuple[bool, str]:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme.lower() in INTERNAL_SCHEMES:
        return True, "internal_scheme"
    if not domain:
        return True, "missing_domain"
    if is_sensitive_domain(domain, deny_domains):
        return True, "sensitive_domain"
    return False, ""


def visit_record(browser: str, row: dict[str, Any], *, strip_query: bool, deny_domains: list[str]) -> tuple[dict[str, Any] | None, str]:
    raw_url = str(row.get("url") or "")
    sanitized_url, domain, query_redacted = sanitize_url(raw_url, strip_query=strip_query)
    skip, reason = should_skip_url(raw_url, domain, deny_domains)
    if skip:
        return None, reason
    dt = chrome_time_to_dt(row.get("last_visit_time")) or datetime.now(UTC)
    title = normalize_space(str(row.get("title") or domain or sanitized_url))[:300]
    visit_count = int(row.get("visit_count") or 0)
    typed_count = int(row.get("typed_count") or 0)
    dedup_key = f"web-visit|{browser}|{dt.date().isoformat()}|{safe_hash(sanitized_url, 20)}"
    content = "\n".join([
        f"网页访问：{title or domain}",
        f"URL：{sanitized_url}",
        f"域名：{domain}",
        f"浏览器：{browser}",
        f"访问次数：{visit_count}",
    ])
    record = {
        "id": f"web-visit-{safe_hash(dedup_key, 20)}",
        "timestamp": dt.isoformat(),
        "source": "web-history",
        "type": "web-visit",
        "role": "system",
        "project": "web",
        "title": title,
        "url": sanitized_url,
        "content": content,
        "metadata": {
            "browser": browser,
            "domain": domain,
            "visit_count": visit_count,
            "typed_count": typed_count,
            "url_query_redacted": query_redacted,
            "capture_scope": "metadata",
            "dedup_key": dedup_key,
        },
        "_dedup_key": dedup_key,
    }
    return record, ""


def fetch_url_text(url: str, *, timeout: int = 20) -> tuple[str, str, str]:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme == "file":
        path = Path(urllib.parse.unquote(parsed.path)).expanduser()
        raw = path.read_text(encoding="utf-8", errors="ignore")
        extractor = TextExtractor()
        extractor.feed(raw)
        title = extractor.title() or path.name
        return title, extractor.text() or normalize_space(raw), url

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "ImmortalMemory/0.2 local web capture",
            "Accept": "text/html,text/plain;q=0.9,*/*;q=0.2",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        final_url = response.geturl()
        raw = response.read(2_000_000)
        charset = response.headers.get_content_charset() or "utf-8"
    text = raw.decode(charset, errors="ignore")
    extractor = TextExtractor()
    extractor.feed(text)
    title = extractor.title()
    body = extractor.text()
    if not body:
        body = normalize_space(re.sub(r"<[^>]+>", " ", text))
    return title, body, final_url


def slugify(value: str, fallback: str = "page") -> str:
    value = normalize_space(value).lower()
    value = re.sub(r"https?://", "", value)
    value = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", value).strip("-")
    return value[:80] or fallback


def save_page(
    vault_dir: Path,
    url: str,
    *,
    note: str = "",
    tags: list[str] | None = None,
    dry_run: bool = False,
    force: bool = False,
    timeout: int = 20,
    max_chars: int = 12000,
    deny_domains: list[str] | None = None,
    strip_query: bool = True,
) -> dict[str, Any]:
    deny_domains = deny_domains or DEFAULT_DENY_DOMAINS
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme.lower() in INTERNAL_SCHEMES and parsed.scheme.lower() != "file":
        raise ValueError(f"blocked internal URL scheme: {parsed.scheme}")
    sanitized_url, domain, query_redacted = sanitize_url(url, strip_query=strip_query)
    if parsed.scheme.lower() != "file" and not force and is_sensitive_domain(domain, deny_domains):
        raise ValueError(f"blocked sensitive domain: {domain}")

    title, body, final_url = fetch_url_text(url, timeout=timeout)
    if final_url and final_url != url:
        sanitized_url, domain, query_redacted = sanitize_url(final_url, strip_query=strip_query)
    title = normalize_space(title)[:300] or domain or "网页正文"
    body = body[:max_chars].strip()
    saved_at = now_iso()
    date = saved_at[:10]
    page_hash = safe_hash(sanitized_url, 12)
    slug = slugify(title, fallback=domain or "page")
    relative_path = f"web/pages/{date}/{page_hash}-{slug}.md"
    dest = vault_dir / relative_path
    dedup_key = f"web-page|{safe_hash(sanitized_url, 24)}"
    record = {
        "id": f"web-page-{safe_hash(dedup_key, 20)}",
        "timestamp": saved_at,
        "source": "web-page",
        "type": "web-page",
        "role": "system",
        "project": "web",
        "title": title,
        "url": sanitized_url,
        "content": "\n".join([
            f"网页正文：{title}",
            f"URL：{sanitized_url}",
            f"域名：{domain}",
            f"备注：{normalize_space(note)}" if note else "",
            "",
            body,
        ]).strip(),
        "metadata": {
            "domain": domain,
            "page_path": relative_path,
            "note": note,
            "tags": tags or [],
            "url_query_redacted": query_redacted,
            "capture_scope": "fulltext",
            "dedup_key": dedup_key,
        },
        "_dedup_key": dedup_key,
    }
    web_index_entry = {
        "saved_at": saved_at,
        "title": title,
        "url": sanitized_url,
        "domain": domain,
        "path": relative_path,
        "note": note,
        "tags": tags or [],
        "dedup_key": dedup_key,
    }
    if dry_run:
        return {"status": "dry-run", "record": record, "path": str(dest), "chars": len(body)}

    dest.parent.mkdir(parents=True, exist_ok=True)
    md = [
        f"# {title}",
        "",
        f"- 保存时间：{saved_at}",
        f"- 来源：{sanitized_url}",
        f"- 域名：{domain or 'unknown'}",
    ]
    if note:
        md.append(f"- 备注：{normalize_space(note)}")
    if tags:
        md.append(f"- 标签：{', '.join(tags)}")
    md.extend(["", body, ""])
    dest.write_text("\n".join(md), encoding="utf-8")

    paths = web_paths(vault_dir)
    paths["web_index"].parent.mkdir(parents=True, exist_ok=True)
    with paths["web_index"].open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(web_index_entry, ensure_ascii=False) + "\n")
    existing = load_existing_dedup(vault_dir, "web-page")
    written_record = False
    if dedup_key not in existing:
        append_records(vault_dir, [record])
        written_record = True
    return {
        "status": "ok",
        "record_written": written_record,
        "path": str(dest),
        "relative_path": relative_path,
        "chars": len(body),
        "title": title,
        "url": sanitized_url,
    }


def collect_history(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config()
    vault_dir = configured_vault_dir(config)
    web_config = config.get("web_capture") if isinstance(config.get("web_capture"), dict) else {}
    enabled = bool(web_config.get("enabled", True))
    paths = web_paths(vault_dir)
    if not enabled:
        status = {
            "generated_at": now_iso(),
            "status": "disabled",
            "totals": {"records_written": 0, "visits_seen": 0, "visits_filtered": 0, "errors": 0},
            "errors": [],
        }
        return status

    max_visits = int(args.limit or web_config.get("max_visits_per_run") or DEFAULT_MAX_VISITS_PER_RUN)
    max_pages = int(web_config.get("max_pages_per_run") or DEFAULT_MAX_PAGES_PER_RUN)
    lookback_hours = float(args.since_hours or web_config.get("history_lookback_hours") or DEFAULT_HISTORY_LOOKBACK_HOURS)
    strip_query = bool(web_config.get("strip_url_query", True))
    deny_domains = list(web_config.get("deny_domains") or DEFAULT_DENY_DOMAINS)
    allow_domains = list(web_config.get("fulltext_allow_domains") or [])
    state = read_json(paths["state"], {})
    since_dt = parse_iso(state.get("last_collect_at")) or (datetime.now(UTC) - timedelta(hours=lookback_hours))
    seen_visit_keys = load_existing_dedup(vault_dir, "web-history")
    records: list[dict[str, Any]] = []
    page_saves: list[dict[str, Any]] = []
    errors: list[str] = []
    browser_stats: list[dict[str, Any]] = []
    visits_seen = 0
    visits_filtered = 0
    duplicates = 0
    denied = 0
    latest_visit: str | None = None

    for browser in browser_profiles(config):
        name = str(browser["name"])
        history_path = Path(browser["history_path"]).expanduser()
        try:
            rows = read_history_rows(history_path, since_dt=since_dt, limit=max_visits)
        except Exception as exc:
            errors.append(f"{name}: {exc}")
            browser_stats.append({"name": name, "history_path": str(history_path), "status": "error", "error": str(exc)})
            continue
        browser_written = 0
        browser_filtered = 0
        for row in rows:
            visits_seen += 1
            record, reason = visit_record(name, row, strip_query=strip_query, deny_domains=deny_domains)
            if not record:
                visits_filtered += 1
                browser_filtered += 1
                if reason == "sensitive_domain":
                    denied += 1
                continue
            latest_visit = max(latest_visit or record["timestamp"], record["timestamp"])
            dedup_key = record["_dedup_key"]
            if dedup_key in seen_visit_keys:
                duplicates += 1
                continue
            seen_visit_keys.add(dedup_key)
            records.append(record)
            browser_written += 1
            domain = (record.get("metadata") or {}).get("domain") or ""
            if allow_domains and domain_matches(str(domain), allow_domains) and len(page_saves) < max_pages and not args.dry_run:
                try:
                    page_saves.append(save_page(
                        vault_dir,
                        str(row.get("url") or ""),
                        note="allowlisted auto capture",
                        dry_run=False,
                        deny_domains=deny_domains,
                        strip_query=strip_query,
                    ))
                except Exception as exc:
                    errors.append(f"{name} fulltext {domain}: {exc}")
        browser_stats.append({
            "name": name,
            "history_path": str(history_path),
            "status": "ok",
            "rows": len(rows),
            "records_written": browser_written,
            "filtered": browser_filtered,
        })

    if not args.dry_run:
        append_records(vault_dir, records)
    total_history_records = count_source_records(vault_dir, "web-history") if not args.dry_run else 0
    total_page_records = count_source_records(vault_dir, "web-page") if not args.dry_run else 0
    generated_at = now_iso()
    status = {
        "generated_at": generated_at,
        "last_collect_at": generated_at,
        "status": "ok" if not errors else "attention",
        "dry_run": bool(args.dry_run),
        "since": since_dt.isoformat(),
        "totals": {
            "visits_seen": visits_seen,
            "visits_filtered": visits_filtered,
            "records_written": 0 if args.dry_run else len(records),
            "records_planned": len(records),
            "total_web_history_records": total_history_records,
            "total_web_page_records": total_page_records,
            "duplicates": duplicates,
            "denied": denied,
            "pages_saved": len(page_saves),
            "errors": len(errors),
            "latest_visit_at": latest_visit or "",
        },
        "browsers": browser_stats,
        "allow_domains": allow_domains,
        "deny_domains_count": len(deny_domains),
        "errors": errors[:20],
    }
    if not args.dry_run:
        paths["web_dir"].mkdir(parents=True, exist_ok=True)
        paths["state"].write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        paths["latest"].write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return status


def command_collect(args: argparse.Namespace) -> int:
    status = collect_history(args)
    if args.json:
        print(json.dumps(status, ensure_ascii=False, indent=2))
    else:
        totals = status.get("totals") or {}
        print(
            "web_collect="
            f"{status.get('status')} "
            f"visits_seen={totals.get('visits_seen', 0)} "
            f"records_written={totals.get('records_written', 0)} "
            f"records_planned={totals.get('records_planned', 0)} "
            f"filtered={totals.get('visits_filtered', 0)} "
            f"errors={totals.get('errors', 0)}"
        )
        for item in status.get("browsers") or []:
            print(
                f"browser={item.get('name')} status={item.get('status')} "
                f"rows={item.get('rows', 0)} written={item.get('records_written', 0)}"
            )
    return 0 if status.get("status") in {"ok", "disabled"} else 2


def command_save(args: argparse.Namespace) -> int:
    config = load_config()
    vault_dir = configured_vault_dir(config)
    web_config = config.get("web_capture") if isinstance(config.get("web_capture"), dict) else {}
    try:
        result = save_page(
            vault_dir,
            args.url,
            note=args.note or "",
            tags=args.tag or [],
            dry_run=args.dry_run,
            force=args.force,
            timeout=args.timeout,
            max_chars=args.max_chars,
            deny_domains=list(web_config.get("deny_domains") or DEFAULT_DENY_DOMAINS),
            strip_query=bool(web_config.get("strip_url_query", True)),
        )
    except (ValueError, OSError, urllib.error.URLError) as exc:
        result = {"status": "error", "error": str(exc), "url": sanitize_url(args.url)[0]}
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else f"web_save=error error={exc}")
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(
            "web_save="
            f"{result.get('status')} "
            f"path={result.get('relative_path') or result.get('path')} "
            f"chars={result.get('chars', 0)}"
        )
    return 0 if result.get("status") in {"ok", "dry-run"} else 2


def command_status(args: argparse.Namespace) -> int:
    config = load_config()
    vault_dir = configured_vault_dir(config)
    paths = web_paths(vault_dir)
    status = read_json(paths["state"], {})
    if not status:
        status = {"status": "missing", "state_path": str(paths["state"])}
    if args.json:
        print(json.dumps(status, ensure_ascii=False, indent=2))
    else:
        totals = status.get("totals") if isinstance(status, dict) else {}
        print(f"web_status={status.get('status', 'missing')}")
        print(f"state={paths['state']}")
        print(f"records_written={(totals or {}).get('records_written', 0)}")
        print(f"latest_visit_at={(totals or {}).get('latest_visit_at', '')}")
        print(f"errors={(totals or {}).get('errors', 0)}")
    return 0 if status.get("status") in {"ok", "disabled", "attention"} else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Capture browser history metadata and manual web snapshots")
    sub = parser.add_subparsers(dest="command", required=True)
    collect = sub.add_parser("collect", help="Collect browser history metadata")
    collect.add_argument("--dry-run", action="store_true")
    collect.add_argument("--limit", type=int, default=None)
    collect.add_argument("--since-hours", type=float, default=None)
    collect.add_argument("--json", action="store_true")
    collect.set_defaults(func=command_collect)

    save = sub.add_parser("save", help="Manually save one web page as a Markdown snapshot")
    save.add_argument("url")
    save.add_argument("--note", default="")
    save.add_argument("--tag", action="append", default=[])
    save.add_argument("--dry-run", action="store_true")
    save.add_argument("--force", action="store_true")
    save.add_argument("--timeout", type=int, default=20)
    save.add_argument("--max-chars", type=int, default=12000)
    save.add_argument("--json", action="store_true")
    save.set_defaults(func=command_save)

    status = sub.add_parser("status", help="Show latest web capture status")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=command_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
