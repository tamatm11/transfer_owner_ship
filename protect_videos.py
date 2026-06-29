"""Protect course videos across subject folders (Toán / Lý / Hóa ...).

Two operations on the VIDEO files found inside one or more Drive folders
(optionally scanned recursively):

  transfer  Move ownership of every video from account A to account B.
            Reuses the consumer (pending-owner) or workspace (direct) flow
            from transfer_ownership.py.

  block     Set Google Drive's "copyRequiresWriterPermission" flag on every
            video. This disables Download / Copy / Print for anyone who only
            has viewer or commenter access — the people who would crawl and
            re-sell your material. Editors/owners are unaffected.

IMPORTANT ordering note
-----------------------
The block flag can only be set by the file's OWNER. Once account A transfers a
video to account B, account A can no longer block it. So either:
  * run `block` with account A BEFORE transferring (the flag survives the
    ownership change and keeps protecting the file under B), or
  * run `block` with account B's token AFTER the transfer.

Examples
--------
  # 1) Block download on A's videos in three subject folders (recursive):
  python protect_videos.py block \
      --token token_A.json --recursive \
      --folder-id <TOAN_ID> --folder-id <LY_ID> --folder-id <HOA_ID>

  # 2) Transfer those same videos from A to B (consumer Gmail, auto-accept):
  python protect_videos.py transfer \
      --owner-token token_A.json --accept-token tools/ownership/token_B.json \
      --to-email accountB@gmail.com --recursive \
      --folder-id <TOAN_ID> --folder-id <LY_ID> --folder-id <HOA_ID>

  # Preview first — nothing is changed:
  python protect_videos.py transfer ... --dry-run
"""

from __future__ import annotations

import argparse
import sys
import time
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
from transfer_ownership import (
    DriveItem,
    OAuthTokenError,
    build_drive_service,
    execute_with_retry,
    list_folder_children,
    get_file,
    transfer_consumer_owner,
    transfer_workspace_owner,
    _http_status,
)


def _error_hint(exc: HttpError) -> str:
    """A short, human-readable hint appended to error lines for common cases."""
    status = _http_status(exc)
    if status == 403:
        return (
            " (HTTP 403 — the token may not OWN this file; if you already "
            "transferred it, block/transfer with account B's token instead)"
        )
    if status == 404:
        return " (HTTP 404 — file not found or no access with this token)"
    return ""


def is_video(item: DriveItem) -> bool:
    """True for actual video files (mimeType video/*)."""
    return item.mime_type.startswith("video/")


def collect_videos(
    service,
    folder_ids: Iterable[str],
    *,
    recursive: bool,
) -> list[DriveItem]:
    """Return every video file inside the given folders.

    Each folder id is walked breadth-first when recursive=True. Shortcuts and
    sub-folders are traversed for discovery but never returned as videos.
    """
    videos: list[DriveItem] = []
    seen_video_ids: set[str] = set()
    visited_folders: set[str] = set()

    for folder_id in folder_ids:
        root = get_file(service, folder_id)
        if root.mime_type != FOLDER_MIME_TYPE:
            # Caller pointed directly at a file; include it if it is a video.
            if is_video(root) and root.id not in seen_video_ids:
                seen_video_ids.add(root.id)
                videos.append(root)
            continue

        queue = [root]
        while queue:
            folder = queue.pop(0)
            if folder.id in visited_folders:
                continue
            visited_folders.add(folder.id)
            print(
                f"[scan] {folder.name} — videos so far: {len(videos)}",
                flush=True,
            )

            for child in list_folder_children(service, folder.id):
                if child.mime_type == FOLDER_MIME_TYPE:
                    if recursive:
                        queue.append(child)
                    continue
                if child.mime_type == SHORTCUT_MIME_TYPE:
                    continue
                if is_video(child) and child.id not in seen_video_ids:
                    seen_video_ids.add(child.id)
                    videos.append(child)

    return videos


# --------------------------------------------------------------------------- #
# transfer
# --------------------------------------------------------------------------- #


def run_transfer(args: argparse.Namespace) -> int:
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
                "[WARN] consumer mode without --accept-token only creates pending-owner "
                "requests; account B still has to accept them manually.",
                file=sys.stderr,
            )

    videos = collect_videos(owner_service, args.folder_id, recursive=args.recursive)
    print(
        f"Found {len(videos)} video(s) across {len(args.folder_id)} folder(s). "
        f"mode={args.mode} dry_run={args.dry_run}"
    )

    success = skipped = failed = 0
    for index, item in enumerate(videos, start=1):
        if args.max_items is not None and index > args.max_items:
            break
        label = f"{item.name} ({item.id})"

        if args.dry_run:
            success += 1
            print(f"[DRY]  {label}")
            continue

        try:
            if args.mode == "workspace":
                transfer_workspace_owner(
                    owner_service, item.id, args.to_email, notify=not args.no_notify
                )
            else:
                transfer_consumer_owner(
                    owner_service,
                    accept_service,
                    item,
                    args.to_email,
                    notify=not args.no_notify,
                )
            success += 1
            print(f"[OK]   {label}")
        except HttpError as exc:
            failed += 1
            print(f"[ERR]  {label}: {exc}{_error_hint(exc)}", file=sys.stderr)

        if args.sleep > 0:
            time.sleep(args.sleep)

    print(f"Done. success={success}, skipped={skipped}, failed={failed}")
    return 1 if failed else 0


# --------------------------------------------------------------------------- #
# block
# --------------------------------------------------------------------------- #


def get_copy_restriction(service, file_id: str) -> bool:
    info = execute_with_retry(
        service.files().get(
            fileId=file_id,
            fields="copyRequiresWriterPermission",
            supportsAllDrives=True,
        )
    )
    return bool(info.get("copyRequiresWriterPermission", False))


def set_copy_restriction(service, file_id: str, *, restricted: bool) -> None:
    execute_with_retry(
        service.files().update(
            fileId=file_id,
            body={"copyRequiresWriterPermission": restricted},
            fields="id,copyRequiresWriterPermission",
            supportsAllDrives=True,
        )
    )


def run_block(args: argparse.Namespace) -> int:
    try:
        service = build_drive_service(args.token)
    except OAuthTokenError as exc:
        print(f"[AUTH ERR] {exc}", file=sys.stderr)
        return 2
    restricted = not args.unblock
    action = "BLOCK" if restricted else "UNBLOCK"

    videos = collect_videos(service, args.folder_id, recursive=args.recursive)
    print(
        f"Found {len(videos)} video(s) across {len(args.folder_id)} folder(s). "
        f"action={action} dry_run={args.dry_run}"
    )

    success = skipped = failed = 0
    for index, item in enumerate(videos, start=1):
        if args.max_items is not None and index > args.max_items:
            break
        label = f"{item.name} ({item.id})"

        if args.dry_run:
            success += 1
            print(f"[DRY]  {action} {label}")
            continue

        try:
            if get_copy_restriction(service, item.id) == restricted:
                skipped += 1
                print(f"[SKIP] {action} {label}: already {action.lower()}ed")
                continue
            set_copy_restriction(service, item.id, restricted=restricted)
            success += 1
            print(f"[OK]   {action} {label}")
        except HttpError as exc:
            failed += 1
            print(f"[ERR]  {label}: {exc}{_error_hint(exc)}", file=sys.stderr)

        if args.sleep > 0:
            time.sleep(args.sleep)

    print(f"Done. {action.lower()}ed={success}, skipped={skipped}, failed={failed}")
    return 1 if failed else 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _add_common_scan_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--folder-id",
        action="append",
        required=True,
        metavar="ID",
        help="Subject folder ID (Toán / Lý / Hóa ...). Repeat for multiple folders.",
    )
    p.add_argument(
        "--recursive",
        action="store_true",
        help="Scan sub-folders too (recommended for nested course structures).",
    )
    p.add_argument(
        "--max-items",
        type=int,
        help="Stop after this many videos (useful for daily quota batching).",
    )
    p.add_argument(
        "--sleep",
        type=float,
        default=1.0,
        help="Seconds to wait between API calls (default: 1.0).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="List the videos that would be changed without changing anything.",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Transfer video ownership A->B and/or block video download.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    t = sub.add_parser("transfer", help="Move video ownership from account A to account B.")
    _add_common_scan_args(t)
    t.add_argument("--to-email", required=True, help="Account B email address.")
    t.add_argument(
        "--owner-token",
        default="token.json",
        help="OAuth token JSON for account A (default: token.json).",
    )
    t.add_argument(
        "--accept-token",
        help="OAuth token JSON for account B. Required to auto-accept consumer transfers.",
    )
    t.add_argument(
        "--credentials",
        default="credentials.json",
        help="OAuth client JSON used when --reauth-accept-token is needed.",
    )
    t.add_argument(
        "--reauth-accept-token",
        action="store_true",
        help=(
            "If --accept-token is expired/revoked, open Chrome/browser login "
            "and overwrite it with a fresh account B token."
        ),
    )
    t.add_argument(
        "--mode",
        choices=("consumer", "workspace"),
        default="consumer",
        help="consumer = pending owner + B accepts; workspace = direct transfer.",
    )
    t.add_argument(
        "--no-notify",
        action="store_true",
        help="Do not send Google email notifications where the API allows it.",
    )
    t.set_defaults(func=run_transfer)

    b = sub.add_parser(
        "block",
        help="Block (or --unblock) Download/Copy/Print of videos for viewers & commenters.",
    )
    _add_common_scan_args(b)
    b.add_argument(
        "--token",
        default="token.json",
        help="OAuth token JSON for the account that OWNS the videos (default: token.json).",
    )
    b.add_argument(
        "--unblock",
        action="store_true",
        help="Reverse the restriction (re-allow download/copy/print).",
    )
    b.set_defaults(func=run_block)

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
