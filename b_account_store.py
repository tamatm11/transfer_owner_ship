"""Persistent storage for account B OAuth token profiles."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path


REGISTRY_VERSION = 1


@dataclass(frozen=True)
class BAccount:
    email: str
    token_path: str
    display_name: str = ""


def load_registry(path: Path) -> tuple[list[BAccount], str]:
    """Load saved accounts and the active email from a registry JSON file."""
    if not path.exists():
        return [], ""

    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, TypeError):
        return [], ""
    if not isinstance(data, dict):
        return [], ""

    accounts: list[BAccount] = []
    seen: set[str] = set()
    raw_accounts = data.get("accounts", [])
    if not isinstance(raw_accounts, list):
        raw_accounts = []
    for raw in raw_accounts:
        if not isinstance(raw, dict):
            continue
        email = str(raw.get("email", "")).strip()
        token_path = str(raw.get("token_path", "")).strip()
        key = email.casefold()
        if not email or not token_path or key in seen:
            continue
        seen.add(key)
        accounts.append(
            BAccount(
                email=email,
                token_path=token_path,
                display_name=str(raw.get("display_name", "")).strip(),
            )
        )

    saved_active = str(data.get("active_email", "")).strip().casefold()
    active_email = next(
        (account.email for account in accounts if account.email.casefold() == saved_active),
        "",
    )
    return accounts, active_email


def save_registry(path: Path, accounts: list[BAccount], active_email: str) -> None:
    """Atomically save account metadata. OAuth secrets remain in token files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "version": REGISTRY_VERSION,
        "active_email": active_email,
        "accounts": [asdict(account) for account in accounts],
    }
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(path)


def managed_token_path(token_dir: Path, email: str) -> Path:
    """Return a stable, filesystem-safe token path for an email address."""
    local_part = email.split("@", 1)[0]
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", local_part).strip("._-")
    slug = slug[:40] or "account"
    digest = hashlib.sha256(email.casefold().encode("utf-8")).hexdigest()[:10]
    return token_dir / f"token_B_{slug}_{digest}.json"


def import_token_file(source: Path, token_dir: Path, email: str) -> Path:
    """Copy an OAuth token into the managed account B token directory."""
    source = source.resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Token file not found: {source}")

    destination = managed_token_path(token_dir, email).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source != destination:
        shutil.copy2(source, destination)
    return destination


def save_token_json(token_dir: Path, email: str, token_json: str) -> Path:
    """Atomically save newly authorized OAuth credentials for an account."""
    destination = managed_token_path(token_dir, email).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination.with_suffix(destination.suffix + ".tmp")
    temp_path.write_text(token_json, encoding="utf-8")
    temp_path.replace(destination)
    return destination


def upsert_account(accounts: list[BAccount], account: BAccount) -> list[BAccount]:
    """Insert or replace an account by case-insensitive email address."""
    key = account.email.casefold()
    updated = [item for item in accounts if item.email.casefold() != key]
    updated.append(account)
    return sorted(updated, key=lambda item: item.email.casefold())
