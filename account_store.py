"""Account registry shared by the web app.

OAuth secrets stay in token files; HTTP callers should expose metadata only.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class Account:
    email: str
    token_path: str
    display_name: str = ""


def load_registry(path: Path) -> tuple[list[Account], str]:
    if not path.is_file():
        return [], ""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, TypeError):
        return [], ""
    if not isinstance(data, dict):
        return [], ""
    accounts: list[Account] = []
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
        accounts.append(Account(email, token_path, str(raw.get("display_name", "")).strip()))
    active_key = str(data.get("active_email", "")).strip().casefold()
    active = next((item.email for item in accounts if item.email.casefold() == active_key), "")
    return sorted(accounts, key=lambda item: item.email.casefold()), active


def save_registry(path: Path, accounts: list[Account], active_email: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"version": 1, "active_email": active_email, "accounts": [asdict(item) for item in accounts]}
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def managed_token_path(token_dir: Path, role: str, email: str) -> Path:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", email.split("@", 1)[0]).strip("._-")
    digest = hashlib.sha256(email.casefold().encode()).hexdigest()[:10]
    return token_dir / f"token_{role.upper()}_{(slug[:40] or 'account')}_{digest}.json"


def save_token_json(token_dir: Path, role: str, email: str, token_json: str) -> Path:
    destination = managed_token_path(token_dir, role, email).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(token_json, encoding="utf-8")
    temporary.replace(destination)
    return destination


def upsert(accounts: list[Account], account: Account) -> list[Account]:
    result = [item for item in accounts if item.email.casefold() != account.email.casefold()]
    result.append(account)
    return sorted(result, key=lambda item: item.email.casefold())


def find_account(accounts: list[Account], email: str) -> Account | None:
    key = email.strip().casefold()
    return next((item for item in accounts if item.email.casefold() == key), None)
