"""Auto-transfer video and folder ownership from account A to account B.

Give it a comma-separated list of Google Drive folder URLs. The tool:
  * extracts the folder id from each URL,
  * scans every folder recursively (by default),
  * finds all video files (mimeType video/*),
  * transfers ownership of each video from account A to account B, and
  * automatically ACCEPTS the transfer on account B (consumer Gmail flow),
    so no manual click is needed.

For consumer Gmail accounts, Drive ownership transfer is a two-step handshake:
account A nominates B as "pending owner", then B accepts. This tool performs
both steps because you supply B's OAuth token too.

Example
-------
  python auto_transfer_videos.py \
      --owner-token token_A.json \
      --accept-token tools/ownership/token_B.json \
      --to-email accountB@gmail.com \
      --folders "https://drive.google.com/drive/folders/1AbC...,https://drive.google.com/drive/folders/2XyZ..."

  # Preview without changing anything:
  python auto_transfer_videos.py ... --dry-run

Notes
-----
  * Recursive scanning is ON by default; pass --no-recursive to scan only the
    top level of each folder.
  * For Google Workspace accounts in the same organisation use --mode workspace
    (direct transfer, no accept step needed).
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterable
from pathlib import Path

# Windows consoles default to cp1252, which cannot encode Vietnamese file names
# (e.g. \u1ea7). Force UTF-8 so printing video titles never crashes.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from googleapiclient.errors import HttpError

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from drive_common import FOLDER_MIME_TYPE, SHORTCUT_MIME_TYPE
from protect_videos import _error_hint, is_blockable_file, is_video
from transfer_ownership import (
    DriveItem,
    ItemOutcome,
    OAuthTokenError,
    ServiceFactory,
    get_authenticated_email,
    get_file,
    list_folder_children,
    owner_skip_reason,
    run_item_batch,
    transfer_consumer_owner,
    transfer_workspace_owner,
    verify_owner,
)


# Folder / file id is the run of id-safe characters after the relevant marker.
_ID_PATTERNS = (
    re.compile(r"/folders/([A-Za-z0-9_-]+)"),
    re.compile(r"/file/d/([A-Za-z0-9_-]+)"),
    re.compile(r"[?&]id=([A-Za-z0-9_-]+)"),
)


def extract_folder_id(value: str) -> str:
    """Return the Drive id embedded in a folder URL, or the value itself.

    Accepts the common Drive URL shapes plus a bare id pasted directly.
    """
    text = value.strip()
    if not text:
        raise ValueError("empty folder reference")

    for pattern in _ID_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1)

    # No URL markers: assume the user pasted a bare id. Reject anything that
    # still looks like a URL so mistakes surface instead of hitting the API.
    if "/" in text or "://" in text:
        raise ValueError(f"could not extract a Drive id from URL: {value!r}")
    return text


def parse_folder_list(raw: str) -> list[str]:
    """Split a comma-separated URL/id list into de-duplicated folder ids."""
    ids: list[str] = []
    seen: set[str] = set()
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        folder_id = extract_folder_id(chunk)
        if folder_id not in seen:
            seen.add(folder_id)
            ids.append(folder_id)
    if not ids:
        raise ValueError("no folder URLs/ids found in --folders")
    return ids


def collect_transfer_items(
    service,
    folder_ids: Iterable[str],
    *,
    recursive: bool,
    transfer_scope: str,
) -> list[DriveItem]:
    """Collect files and/or folders in a transfer-safe order.

    With scope ``videos`` only video files are selected; ``files`` selects every
    non-folder file (Word, PDF, docx, …); ``all`` adds folders too. Files are
    returned first. Folders are returned deepest-first so changing ownership of a
    parent folder cannot interrupt traversal or child updates.
    """
    include_files = transfer_scope in {"videos", "files", "all"}
    include_folders = transfer_scope in {"folders", "all"}
    accept_file = is_video if transfer_scope == "videos" else is_blockable_file
    files: list[DriveItem] = []
    folders_with_depth: list[tuple[int, DriveItem]] = []
    seen_files: set[str] = set()
    seen_folders: set[str] = set()

    for folder_id in folder_ids:
        root = get_file(service, folder_id)
        if root.mime_type != FOLDER_MIME_TYPE:
            if include_files and accept_file(root) and root.id not in seen_files:
                seen_files.add(root.id)
                files.append(root)
            continue

        queue: list[tuple[DriveItem, int]] = [(root, 0)]
        while queue:
            folder, depth = queue.pop(0)
            if folder.id in seen_folders:
                continue
            seen_folders.add(folder.id)
            if include_folders:
                folders_with_depth.append((depth, folder))

            print(
                f"[scan] {folder.name} - files={len(files)} "
                f"folders={len(folders_with_depth)}",
                flush=True,
            )
            for child in list_folder_children(service, folder.id):
                if child.mime_type == FOLDER_MIME_TYPE:
                    if recursive:
                        queue.append((child, depth + 1))
                    continue
                if child.mime_type == SHORTCUT_MIME_TYPE:
                    continue
                if include_files and accept_file(child) and child.id not in seen_files:
                    seen_files.add(child.id)
                    files.append(child)

    folders = [
        item
        for _, item in sorted(
            folders_with_depth,
            key=lambda entry: entry[0],
            reverse=True,
        )
    ]
    if transfer_scope == "folders":
        return folders
    if transfer_scope == "all":
        return files + folders
    return files


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Auto-transfer video/folder ownership A->B from folder URLs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--folders",
        required=True,
        metavar="URLS",
        help='Comma-separated Drive folder URLs (or ids), e.g. "https://...,https://...".',
    )
    parser.add_argument("--to-email", required=True, help="Account B email address.")
    parser.add_argument(
        "--owner-token",
        default="token.json",
        help="OAuth token JSON for account A (default: token.json).",
    )
    parser.add_argument(
        "--accept-token",
        help="OAuth token JSON for account B. Required so B auto-accepts (consumer mode).",
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
        help="consumer = pending owner + B auto-accept; workspace = direct transfer.",
    )
    parser.add_argument(
        "--transfer-scope",
        choices=("videos", "files", "folders", "all"),
        default="videos",
        help=(
            "Transfer videos only, all files (word/pdf/docx/…), folders only, "
            "or everything (default: videos)."
        ),
    )
    parser.add_argument(
        "--no-recursive",
        action="store_true",
        help="Scan only the top level of each folder (default: recurse into sub-folders).",
    )
    parser.add_argument(
        "--max-items",
        type=int,
        help="Stop after this many selected items (useful for quota batching).",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.0,
        help=(
            "Seconds each worker waits after every API call to respect rate "
            "limits (default: 0.0; raise if you hit HTTP 429)."
        ),
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Number of parallel transfer threads (default: 4, max 16).",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="After each transfer, confirm account B is the real owner.",
    )
    parser.add_argument(
        "--no-notify",
        action="store_true",
        help="Do not send Google email notifications where the API allows it.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List the selected items without changing ownership.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    folder_ids = parse_folder_list(args.folders)
    workers = max(1, min(args.workers, 16))
    try:
        owner_factory = ServiceFactory(args.owner_token)
        accept_factory = (
            ServiceFactory(
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

    owner_service = owner_factory.primary
    accept_service = accept_factory.primary if accept_factory else None

    if args.mode == "consumer" and accept_service is None:
        if args.dry_run:
            print(
                "[WARN] dry-run: account B token was not checked because no "
                "ownership accept calls will be made.",
                file=sys.stderr,
            )
        else:
            print(
                "[WARN] consumer mode without --accept-token only creates pending-owner "
                "requests; account B will NOT auto-accept. Pass --accept-token "
                "tools/ownership/token_B.json or another account B token.",
                file=sys.stderr,
            )

    recursive = not args.no_recursive
    expected_owner_email = get_authenticated_email(owner_service)
    items = collect_transfer_items(
        owner_service,
        folder_ids,
        recursive=recursive,
        transfer_scope=args.transfer_scope,
    )
    if args.max_items is not None:
        items = items[: args.max_items]
    file_count = sum(item.mime_type != FOLDER_MIME_TYPE for item in items)
    folder_count = sum(item.mime_type == FOLDER_MIME_TYPE for item in items)
    print(
        f"Resolved {len(folder_ids)} folder(s). "
        f"Selected {file_count} file(s), {folder_count} folder(s). "
        f"scope={args.transfer_scope} mode={args.mode} "
        f"recursive={recursive} workers={workers} verify={args.verify} "
        f"owner_filter={expected_owner_email or 'unknown'} "
        f"dry_run={args.dry_run}"
    )

    notify = not args.no_notify

    def process_one(item: DriveItem) -> ItemOutcome:
        label = f"{item.name} ({item.id})"
        owner_reason = owner_skip_reason(
            item,
            expected_owner_email,
            already_owner_email=args.to_email,
        )
        if owner_reason:
            return ItemOutcome("skip", f"[SKIP] {label}: {owner_reason}")
        if args.dry_run:
            return ItemOutcome("ok", f"[DRY]  {label}")
        try:
            if args.mode == "workspace":
                transfer_workspace_owner(
                    owner_factory.get(), item.id, args.to_email, notify=notify
                )
                check_service = owner_factory.get()
            else:
                transfer_consumer_owner(
                    owner_factory.get(),
                    accept_factory.get() if accept_factory else None,
                    item,
                    args.to_email,
                    notify=notify,
                )
                check_service = (
                    accept_factory.get() if accept_factory else owner_factory.get()
                )
            if args.verify and not verify_owner(check_service, item.id, args.to_email):
                return ItemOutcome(
                    "fail",
                    f"[ERR]  {label}: verify failed — {args.to_email} is not the "
                    f"confirmed owner yet",
                )
            return ItemOutcome("ok", f"[OK]   {label}")
        except HttpError as exc:
            return ItemOutcome("fail", f"[ERR]  {label}: {exc}{_error_hint(exc)}")

    counts = run_item_batch(
        items,
        process_one,
        workers=1 if args.dry_run else workers,
        sleep_seconds=args.sleep,
    )

    print(
        f"Done. transferred={counts['ok']}, "
        f"skipped={counts['skip']}, failed={counts['fail']}"
    )
    return 1 if counts["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
