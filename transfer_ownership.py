"""Transfer Google Drive file ownership from account A to account B.

Supports two Google Drive ownership flows:

1. Google Workspace accounts in the same organization:
   account A can transfer ownership directly.

2. Consumer Gmail accounts:
   account A creates a pending-owner permission, then account B must accept it.
   This script can automate the accept step when you provide B's OAuth token.
"""

from __future__ import annotations

import argparse
import http.client
import json
import socket
import ssl
import sys
import threading
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from drive_common import FOLDER_MIME_TYPE, SHORTCUT_MIME_TYPE


SCOPES = ["https://www.googleapis.com/auth/drive"]

# Transient Drive errors worth retrying with backoff (rate limits / server hiccups).
RETRYABLE_STATUS = {429, 500, 502, 503}

# Transient transport failures worth retrying. These surface from
# request.execute() — INCLUDING the implicit OAuth token refresh that runs just
# before the HTTP call — and are not HttpError, so they must be caught
# separately. ssl.SSLEOFError ("EOF occurred in violation of protocol") is a
# subclass of ssl.SSLError; ConnectionError/socket.timeout cover resets and
# timeouts; httplib2 raises http.client.HTTPException for dropped responses.
RETRYABLE_NETWORK_ERRORS: tuple[type[BaseException], ...] = (
    ssl.SSLError,
    socket.timeout,
    socket.gaierror,
    ConnectionError,
    http.client.HTTPException,
    TimeoutError,
    TransportError,
)

try:  # httplib2 transport errors (used under the hood by google-api-python-client)
    from httplib2 import HttpLib2Error

    RETRYABLE_NETWORK_ERRORS += (HttpLib2Error,)
except Exception:  # noqa: BLE001 - httplib2 always present, but stay defensive
    pass


@dataclass
class DriveItem:
    id: str
    name: str
    mime_type: str
    owner_emails: tuple[str, ...] = ()
    owned_by_me: bool | None = None


class OAuthTokenError(RuntimeError):
    """Raised when a token cannot be loaded, refreshed, or re-authorized."""

    def __init__(self, token_path: str, message: str) -> None:
        self.token_path = token_path
        super().__init__(f"{message} Token: {token_path}")


def _http_status(exc: HttpError) -> int | None:
    """Best-effort numeric HTTP status from an HttpError across client versions."""
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "resp", None), "status", None)
    try:
        return int(status)
    except (TypeError, ValueError):
        return None


def _sleep_backoff(attempt: int, retries: int, base_delay: float, reason: str) -> None:
    delay = base_delay * (2 ** attempt)
    print(
        f"[retry] {reason}; attempt {attempt + 1}/{retries}, waiting {delay:.0f}s",
        file=sys.stderr,
        flush=True,
    )
    time.sleep(delay)


def _refresh_credentials(creds: Credentials, *, retries: int = 4, base_delay: float = 2.0) -> None:
    """Refresh an access token, retrying transient network/SSL failures.

    A RefreshError (e.g. invalid_grant) is permanent and re-raised immediately;
    only transport-level drops like SSLEOFError are retried.
    """
    for attempt in range(retries + 1):
        try:
            creds.refresh(Request())
            return
        except RefreshError:
            raise
        except RETRYABLE_NETWORK_ERRORS as exc:
            if attempt == retries:
                raise
            _sleep_backoff(attempt, retries, base_delay, f"refresh {type(exc).__name__}: {exc}")


def execute_with_retry(
    request: Any,
    *,
    retries: int = 4,
    base_delay: float = 2.0,
    retry_404: bool = False,
):
    """Execute a Drive API request, retrying transient failures with backoff.

    retry_404 is used for the consumer accept step, where the freshly created
    pending-owner permission can take a moment to become visible to account B.
    """
    for attempt in range(retries + 1):
        try:
            return request.execute()
        except HttpError as exc:
            status = _http_status(exc)
            retryable = status in RETRYABLE_STATUS or (retry_404 and status == 404)
            if not retryable or attempt == retries:
                raise
            _sleep_backoff(attempt, retries, base_delay, f"HTTP {status}")
        except RETRYABLE_NETWORK_ERRORS as exc:
            # Transient network/SSL drop (e.g. SSLEOFError during token refresh,
            # connection reset). Retrying the request re-runs the refresh too.
            if attempt == retries:
                raise
            _sleep_backoff(
                attempt, retries, base_delay, f"{type(exc).__name__}: {exc}"
            )


def _token_refresh_error_message(exc: RefreshError) -> str:
    text = str(exc)
    if "invalid_grant" in text:
        return (
            "OAuth token has expired or been revoked. Re-login this Google "
            "account to create a fresh token."
        )
    return f"OAuth token refresh failed: {exc}"


def load_credentials(token_path: str) -> Credentials:
    path = Path(token_path)
    try:
        with path.open("r", encoding="utf-8") as f:
            info = json.load(f)
    except FileNotFoundError as exc:
        raise OAuthTokenError(token_path, "OAuth token file was not found.") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise OAuthTokenError(token_path, f"Could not read OAuth token JSON: {exc}") from exc

    try:
        creds = Credentials.from_authorized_user_info(info, SCOPES)
    except ValueError as exc:
        raise OAuthTokenError(token_path, f"OAuth token JSON is invalid: {exc}") from exc

    if not creds.valid and creds.expired and creds.refresh_token:
        try:
            _refresh_credentials(creds)
        except RefreshError as exc:
            raise OAuthTokenError(token_path, _token_refresh_error_message(exc)) from exc
        except RETRYABLE_NETWORK_ERRORS as exc:
            raise OAuthTokenError(
                token_path,
                f"Network error while refreshing token ({type(exc).__name__}): {exc}. "
                "This is usually transient — re-run the job.",
            ) from exc
        info.update(
            {
                "token": creds.token,
                "expiry": creds.expiry.isoformat().replace("+00:00", "Z")
                if creds.expiry
                else info.get("expiry"),
            }
        )
        with path.open("w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False, indent=2)

    if not creds.valid:
        raise OAuthTokenError(
            token_path,
            "OAuth token is invalid or expired. Re-login this Google account.",
        )

    return creds


def reauthorize_credentials(
    credentials_path: str,
    token_path: str,
    *,
    expected_email: str | None = None,
) -> Credentials:
    credentials_file = Path(credentials_path)
    if not credentials_file.is_file():
        raise OAuthTokenError(
            token_path,
            f"Cannot re-authorize because credentials.json was not found: {credentials_file}",
        )

    target = expected_email or "(selected account)"
    print(
        "[AUTH] Token needs re-authorization.\n"
        f"[AUTH] Account to sign in: {target}\n"
        f"[AUTH] Token file: {token_path}\n"
        "[AUTH] Copy the URL below into the Chrome profile for that account.",
        file=sys.stderr,
        flush=True,
    )
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow

        flow = InstalledAppFlow.from_client_secrets_file(
            str(credentials_file), scopes=SCOPES
        )
        creds = flow.run_local_server(
            host="localhost",
            port=0,
            open_browser=False,
            authorization_prompt_message=(
                "\n[AUTH URL]\n{url}\n\n"
                "[AUTH] Waiting for Google login to finish...\n"
            ),
            success_message=(
                "Login completed. You can close this tab and return to the Drive tool."
            ),
            access_type="offline",
            prompt="select_account consent",
        )
    except Exception as exc:
        raise OAuthTokenError(
            token_path,
            f"OAuth re-authorization did not complete: {exc}",
        ) from exc

    if expected_email:
        try:
            service = build("drive", "v3", credentials=creds, cache_discovery=False)
            user = (
                service.about()
                .get(fields="user(emailAddress)")
                .execute()
                .get("user", {})
            )
            actual_email = str(user.get("emailAddress", "")).strip()
        except Exception as exc:
            raise OAuthTokenError(
                token_path,
                f"Could not verify the re-authorized account email: {exc}",
            ) from exc

        if actual_email.casefold() != expected_email.casefold():
            raise OAuthTokenError(
                token_path,
                (
                    f"Signed in as {actual_email or '(unknown email)'}, "
                    f"but this batch expects {expected_email}. Token was not overwritten."
                ),
            )

    token_info = json.loads(creds.to_json())
    if not token_info.get("refresh_token"):
        raise OAuthTokenError(
            token_path,
            (
                "Google did not return a refresh token, so this login would "
                "expire quickly. Remove the app's access in Google Account "
                "permissions, then add the account again."
            ),
        )

    path = Path(token_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(token_info, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp_path.replace(path)
    print(f"[AUTH] Saved fresh token: {token_path}", file=sys.stderr, flush=True)
    return creds


def build_drive_service(
    token_path: str,
    *,
    reauth: bool = False,
    credentials_path: str = "credentials.json",
    expected_email: str | None = None,
):
    try:
        creds = load_credentials(token_path)
    except OAuthTokenError as exc:
        if not reauth:
            raise
        print(f"[AUTH] {exc}", file=sys.stderr, flush=True)
        creds = reauthorize_credentials(
            credentials_path,
            token_path,
            expected_email=expected_email,
        )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


class ServiceFactory:
    """Hand out a Drive service per worker thread.

    google-api-python-client builds on httplib2, whose Http object is NOT
    thread-safe (one shared TCP connection), so each thread needs its own
    service. Any interactive re-authorization / token refresh happens ONCE in
    the constructing thread (``primary``); worker threads only load the
    already-fresh token from disk, so no two threads ever open a browser login
    or race on the token file.
    """

    def __init__(
        self,
        token_path: str,
        *,
        reauth: bool = False,
        credentials_path: str = "credentials.json",
        expected_email: str | None = None,
    ) -> None:
        self.token_path = token_path
        # Warm up on the calling thread: trigger reauth/refresh + token persist
        # now, while we are still single-threaded.
        self.primary = build_drive_service(
            token_path,
            reauth=reauth,
            credentials_path=credentials_path,
            expected_email=expected_email,
        )
        self._local = threading.local()
        self._local.service = self.primary

    def get(self) -> Any:
        service = getattr(self._local, "service", None)
        if service is None:
            # Token is already fresh on disk (warmed up above); never reauth in
            # a worker thread. Auto-refresh after this stays in-memory per
            # thread, so concurrent workers don't race on the token file.
            service = build_drive_service(self.token_path)
            self._local.service = service
        return service


def verify_owner(service: Any, file_id: str, email: str) -> bool:
    """Return True if `email` is the confirmed (non-pending) owner of the file."""
    permission = find_permission(service, file_id, email)
    return bool(
        permission
        and permission.get("role") == "owner"
        and not permission.get("pendingOwner")
    )


def get_authenticated_email(service: Any) -> str:
    """Return the email address behind the authenticated Drive service."""
    user = execute_with_retry(service.about().get(fields="user(emailAddress)")).get("user", {})
    return str(user.get("emailAddress", "")).strip()


def _drive_item_from_payload(item: Mapping[str, Any]) -> DriveItem:
    owners = tuple(
        str(owner.get("emailAddress", "")).strip()
        for owner in item.get("owners", []) or []
        if str(owner.get("emailAddress", "")).strip()
    )
    owned_by_me = item.get("ownedByMe")
    return DriveItem(
        id=item["id"],
        name=item.get("name", ""),
        mime_type=item.get("mimeType", ""),
        owner_emails=owners,
        owned_by_me=bool(owned_by_me) if owned_by_me is not None else None,
    )


def describe_item_owners(item: DriveItem) -> str:
    return ", ".join(item.owner_emails) if item.owner_emails else "unknown owner"


def owner_skip_reason(
    item: DriveItem,
    expected_owner_email: str,
    *,
    already_owner_email: str | None = None,
) -> str | None:
    """Skip files whose current owner is not the selected source account.

    If Drive does not return owner metadata, leave the item in the batch and let
    the transfer API decide. This avoids false skips for unusual Drive items.
    """
    expected = expected_owner_email.strip().casefold()
    if not expected:
        return None
    owners = {email.casefold() for email in item.owner_emails if email}
    if not owners or expected in owners:
        return None
    already_owner = already_owner_email.strip().casefold() if already_owner_email else ""
    if already_owner and already_owner in owners:
        return f"already owned by target {already_owner_email}"
    if owners:
        return f"owner is {describe_item_owners(item)}; expected {expected_owner_email}"
    return None


@dataclass
class ItemOutcome:
    """Result of processing one Drive item in a (possibly parallel) batch."""

    status: str  # "ok" | "skip" | "fail"
    message: str = ""


def run_item_batch(
    items: list[Any],
    process_one: Callable[[Any], ItemOutcome],
    *,
    workers: int,
    sleep_seconds: float = 0.0,
) -> dict[str, int]:
    """Run ``process_one`` over ``items``, optionally across a thread pool.

    ``process_one`` must be self-contained and thread-safe (build its Drive
    service via a :class:`ServiceFactory`). It returns an :class:`ItemOutcome`.
    Logging is serialized so [OK]/[ERR] lines never interleave mid-line, and the
    aggregate ok/skip/fail counts are returned.
    """
    counts = {"ok": 0, "skip": 0, "fail": 0}
    emit_lock = threading.Lock()

    def emit(outcome: ItemOutcome) -> None:
        with emit_lock:
            counts[outcome.status] = counts.get(outcome.status, 0) + 1
            if outcome.message:
                stream = sys.stderr if outcome.status == "fail" else sys.stdout
                print(outcome.message, file=stream, flush=True)

    def work(item: Any) -> ItemOutcome:
        outcome = process_one(item)
        # Throttle per worker so a wide pool still respects rate limits.
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
        return outcome

    if workers <= 1:
        for item in items:
            emit(work(item))
        return counts

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(work, item): item for item in items}
        for future in as_completed(futures):
            try:
                emit(future.result())
            except Exception as exc:  # noqa: BLE001 - keep the batch going
                emit(ItemOutcome("fail", f"[ERR]  batch worker crashed: {exc}"))
    return counts


def get_file(service: Any, file_id: str) -> DriveItem:
    item = (
        service.files()
        .get(
            fileId=file_id,
            fields="id,name,mimeType,owners(emailAddress),ownedByMe",
            supportsAllDrives=True,
        )
        .execute()
    )
    return _drive_item_from_payload(item)


def list_folder_children(service: Any, folder_id: str) -> list[DriveItem]:
    items: list[DriveItem] = []
    page_token = None

    while True:
        resp = (
            service.files()
            .list(
                q=f"'{folder_id}' in parents and trashed=false",
                fields="nextPageToken,files(id,name,mimeType,owners(emailAddress),ownedByMe)",
                pageSize=1000,
                pageToken=page_token,
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
            )
            .execute()
        )
        for item in resp.get("files", []):
            items.append(_drive_item_from_payload(item))
        page_token = resp.get("nextPageToken")
        if not page_token:
            return items


def walk_items(service: Any, root_id: str, *, recursive: bool) -> list[DriveItem]:
    root = get_file(service, root_id)
    items = [root]
    if not recursive or root.mime_type != FOLDER_MIME_TYPE:
        return items

    queue = [root]
    while queue:
        folder = queue.pop(0)
        for child in list_folder_children(service, folder.id):
            items.append(child)
            if child.mime_type == FOLDER_MIME_TYPE:
                queue.append(child)
    return items


def find_permission(service: Any, file_id: str, email: str) -> dict | None:
    """Return the full permission dict for `email` on the file, or None.

    Includes role/pendingOwner so callers can detect a file that is already
    owned by the target account (e.g. when re-running a partial batch).
    """
    page_token = None
    target = email.casefold()

    while True:
        resp = execute_with_retry(
            service.permissions().list(
                fileId=file_id,
                fields="nextPageToken,permissions(id,emailAddress,type,role,pendingOwner)",
                pageToken=page_token,
                supportsAllDrives=True,
            )
        )
        for permission in resp.get("permissions", []):
            if str(permission.get("emailAddress", "")).casefold() == target:
                return permission
        page_token = resp.get("nextPageToken")
        if not page_token:
            return None


def find_permission_id(service: Any, file_id: str, email: str) -> str | None:
    permission = find_permission(service, file_id, email)
    return permission.get("id") if permission else None


def create_pending_owner(service: Any, file_id: str, email: str, *, notify: bool) -> str:
    # Google rejects sendNotificationEmail=False for consumer (gmail) ownership
    # transfers with HTTP 400, so a pending-owner nomination ALWAYS notifies.
    # `notify` is kept for signature symmetry but cannot disable this email.
    del notify
    permission = execute_with_retry(
        service.permissions().create(
            fileId=file_id,
            body={
                "type": "user",
                "role": "writer",
                "emailAddress": email,
                "pendingOwner": True,
            },
            sendNotificationEmail=True,
            fields="id,emailAddress,role,pendingOwner",
            supportsAllDrives=True,
        )
    )
    return permission["id"]


def update_pending_owner(service: Any, file_id: str, permission_id: str, *, notify: bool) -> None:
    # permissions().update() does NOT accept sendNotificationEmail (only
    # permissions().create() does). The pending-owner nomination email is
    # already sent at create time, so update just refreshes the role/flag.
    del notify
    execute_with_retry(
        service.permissions().update(
            fileId=file_id,
            permissionId=permission_id,
            body={"role": "writer", "pendingOwner": True},
            fields="id,emailAddress,role,pendingOwner",
            supportsAllDrives=True,
        )
    )


def accept_ownership(
    service: Any,
    file_id: str,
    permission_id: str,
    email: str,
    *,
    notify: bool,
) -> str:
    # The pending-owner permission can lag a moment before B's service sees it,
    # so tolerate a transient 404 here in addition to the usual rate limits.
    try:
        permission = execute_with_retry(
            service.permissions().update(
                fileId=file_id,
                permissionId=permission_id,
                body={"role": "owner"},
                transferOwnership=True,
                fields="id,emailAddress,role",
                supportsAllDrives=True,
            ),
            retry_404=True,
        )
        return permission.get("id", permission_id)
    except HttpError as exc:
        if _http_status(exc) != 404:
            raise

    # Drive's consumer flow lets the prospective owner accept by creating or
    # updating their permission. Some accounts do not see the pending permission
    # by id immediately, even after retries, so fall back to create.
    permission = execute_with_retry(
        service.permissions().create(
            fileId=file_id,
            body={"type": "user", "role": "owner", "emailAddress": email},
            transferOwnership=True,
            sendNotificationEmail=notify,
            fields="id,emailAddress,role",
            supportsAllDrives=True,
        ),
        retry_404=True,
    )
    return permission.get("id", permission_id)


def transfer_workspace_owner(service: Any, file_id: str, email: str, *, notify: bool) -> str:
    existing = find_permission(service, file_id, email)
    if existing and existing.get("role") == "owner" and not existing.get("pendingOwner"):
        # Already owned by B — idempotent no-op for re-runs.
        return existing.get("id", "")

    if existing:
        permission_id = existing["id"]
        execute_with_retry(
            service.permissions().update(
                fileId=file_id,
                permissionId=permission_id,
                body={"role": "owner"},
                transferOwnership=True,
                sendNotificationEmail=notify,
                fields="id,emailAddress,role",
                supportsAllDrives=True,
            )
        )
        return permission_id

    permission = execute_with_retry(
        service.permissions().create(
            fileId=file_id,
            body={"type": "user", "role": "owner", "emailAddress": email},
            transferOwnership=True,
            sendNotificationEmail=notify,
            fields="id,emailAddress,role",
            supportsAllDrives=True,
        )
    )
    return permission["id"]


def transfer_consumer_owner(
    owner_service: Any,
    accept_service: Any | None,
    item: DriveItem,
    email: str,
    *,
    notify: bool,
) -> str:
    existing = find_permission(owner_service, item.id, email)

    # Already owned by B (common when re-running a batch that partly succeeded).
    # Downgrading the owner back to a pending writer would raise HTTP 403, so
    # treat this as a successful no-op instead.
    if existing and existing.get("role") == "owner" and not existing.get("pendingOwner"):
        return existing.get("id", "")

    if existing:
        permission_id = existing["id"]
        update_pending_owner(owner_service, item.id, permission_id, notify=notify)
    else:
        permission_id = create_pending_owner(owner_service, item.id, email, notify=notify)

    if accept_service is not None:
        permission_id = accept_ownership(
            accept_service,
            item.id,
            permission_id,
            email,
            notify=notify,
        )

    return permission_id


def should_skip_item(item: DriveItem, *, include_shortcuts: bool) -> str | None:
    if item.mime_type == SHORTCUT_MIME_TYPE and not include_shortcuts:
        return "shortcut"
    return None


def transfer_items(
    items: Iterable[DriveItem],
    *,
    mode: str,
    owner_service: Any,
    accept_service: Any | None,
    expected_owner_email: str,
    to_email: str,
    notify: bool,
    dry_run: bool,
    sleep_seconds: float,
    max_items: int | None,
    include_shortcuts: bool,
) -> tuple[int, int, int]:
    success = 0
    skipped = 0
    failed = 0

    for index, item in enumerate(items, start=1):
        if max_items is not None and index > max_items:
            break

        skip_reason = should_skip_item(item, include_shortcuts=include_shortcuts)
        label = f"{item.name} ({item.id})"
        if skip_reason:
            skipped += 1
            print(f"[SKIP] {label}: {skip_reason}")
            continue
        owner_reason = owner_skip_reason(
            item,
            expected_owner_email,
            already_owner_email=to_email,
        )
        if owner_reason:
            skipped += 1
            print(f"[SKIP] {label}: {owner_reason}")
            continue

        if dry_run:
            success += 1
            print(f"[DRY]  {label}")
            continue

        try:
            if mode == "workspace":
                transfer_workspace_owner(owner_service, item.id, to_email, notify=notify)
            else:
                transfer_consumer_owner(
                    owner_service,
                    accept_service,
                    item,
                    to_email,
                    notify=notify,
                )
            success += 1
            print(f"[OK]   {label}")
        except HttpError as exc:
            failed += 1
            print(f"[ERR]  {label}: {exc}", file=sys.stderr)

        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    return success, skipped, failed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Transfer Google Drive ownership from account A to account B."
    )
    parser.add_argument("--file-id", required=True, help="File or folder ID owned by account A")
    parser.add_argument("--to-email", required=True, help="Account B email address")
    parser.add_argument(
        "--owner-token",
        default="token.json",
        help="OAuth token JSON for account A (default: token.json)",
    )
    parser.add_argument(
        "--accept-token",
        help="OAuth token JSON for account B. Required to auto-accept consumer Gmail transfers.",
    )
    parser.add_argument(
        "--credentials",
        default="credentials.json",
        help="OAuth client JSON used when --reauth-accept-token is needed.",
    )
    parser.add_argument(
        "--reauth-accept-token",
        action="store_true",
        help=(
            "If --accept-token is expired/revoked, open Chrome/browser login "
            "and overwrite it with a fresh account B token."
        ),
    )
    parser.add_argument(
        "--mode",
        choices=("consumer", "workspace"),
        default="consumer",
        help="consumer = pending owner + optional B accept; workspace = direct transfer",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="If --file-id is a folder, transfer the folder and all children.",
    )
    parser.add_argument(
        "--max-items",
        type=int,
        help="Stop after this many discovered items. Useful for daily quota batching.",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=1.0,
        help="Seconds to wait between files (default: 1.0)",
    )
    parser.add_argument(
        "--no-notify",
        action="store_true",
        help="Do not send Google email notifications where the API allows it.",
    )
    parser.add_argument(
        "--include-shortcuts",
        action="store_true",
        help="Try to transfer shortcut files too. By default shortcuts are skipped.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be transferred without changing permissions.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        owner_service = build_drive_service(args.owner_token)
        accept_service = (
            build_drive_service(
                args.accept_token,
                reauth=args.reauth_accept_token,
                credentials_path=args.credentials,
                expected_email=args.to_email,
            )
            if args.accept_token and not args.dry_run
            else None
        )
    except OAuthTokenError as exc:
        print(f"[AUTH ERR] {exc}", file=sys.stderr)
        return 2

    if args.mode == "consumer" and accept_service is None:
        if args.dry_run:
            print(
                "[WARN] dry-run: account B token was not checked because no "
                "ownership accept calls will be made.",
                file=sys.stderr,
            )
        else:
            print(
                "[WARN] Consumer mode without --accept-token only creates pending owner requests. "
                "Account B still needs to accept them.",
                file=sys.stderr,
            )

    expected_owner_email = get_authenticated_email(owner_service)
    items = walk_items(owner_service, args.file_id, recursive=args.recursive)
    print(
        f"Discovered {len(items)} item(s). Mode={args.mode}. "
        f"Owner filter={expected_owner_email or 'unknown'}. Dry-run={args.dry_run}."
    )

    success, skipped, failed = transfer_items(
        items,
        mode=args.mode,
        owner_service=owner_service,
        accept_service=accept_service,
        expected_owner_email=expected_owner_email,
        to_email=args.to_email,
        notify=not args.no_notify,
        dry_run=args.dry_run,
        sleep_seconds=args.sleep,
        max_items=args.max_items,
        include_shortcuts=args.include_shortcuts,
    )
    print(f"Done. success={success}, skipped={skipped}, failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
