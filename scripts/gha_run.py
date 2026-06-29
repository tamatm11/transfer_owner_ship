"""GitHub Actions runner for Owner Video Tool jobs.

Reads a job payload (JSON, from env GHA_JOB_PAYLOAD), decodes the Google OAuth
token bundle (env OWNER_TOOL_ACCOUNTS_JSON_B64 — the same value used on Vercel),
materializes each token to a temp file, then runs the existing CLI
(auto_transfer_videos.py / protect_videos.py) once per row.

Tokens never touch the repo: they are written to a temp dir that the runner VM
discards when the job ends. stdout is streamed and captured by GitHub Actions.
"""
import base64
import json
import os
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent


def log(message):
    print(message, flush=True)


def load_bundle():
    raw = os.environ.get("OWNER_TOOL_ACCOUNTS_JSON_B64", "").strip()
    if not raw:
        raise SystemExit("Thiếu secret OWNER_TOOL_ACCOUNTS_JSON_B64.")
    try:
        return json.loads(base64.b64decode(raw).decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"OWNER_TOOL_ACCOUNTS_JSON_B64 không hợp lệ: {exc}")


def materialize_tokens(bundle):
    """Write each account token to a temp file. Returns {(role, email): path}."""
    root = pathlib.Path(tempfile.mkdtemp(prefix="gha_tokens_"))
    paths = {}
    for role in ("A", "B"):
        for acc in bundle.get(role, []) or []:
            email = str(acc.get("email", "")).strip()
            token_b64 = acc.get("token_b64")
            if not email or not token_b64:
                continue
            safe = email.replace("@", "_at_").replace("/", "_")
            target = root / f"{role}_{safe}.json"
            target.write_bytes(base64.b64decode(token_b64))
            paths[(role, email.lower())] = str(target)
    return paths


def token_path(paths, role, email):
    path = paths.get((role, str(email).strip().lower()))
    if not path:
        raise SystemExit(f"Không tìm thấy token cho account {role}: {email}")
    return path


def run(cmd):
    log("$ " + " ".join(cmd[2:]))  # skip python -u for readability
    return subprocess.call(cmd, cwd=str(ROOT))


def run_transfer(payload, paths):
    owner_email = payload["owner_email"]
    owner_token = token_path(paths, "A", owner_email)
    mode = payload.get("mode", "consumer")
    scope = payload.get("scope", "videos")
    rows = payload.get("rows", [])
    if not rows:
        raise SystemExit("Payload transfer không có dòng nào.")
    failures = 0
    for index, row in enumerate(rows, start=1):
        to_email = row["receiver_email"]
        folders = ",".join(row.get("folders", []))
        if not folders:
            log(f"[skip] Dòng {index} ({to_email}) không có folder.")
            failures += 1
            continue
        cmd = [
            sys.executable, "-u", "auto_transfer_videos.py",
            "--folders", folders,
            "--to-email", to_email,
            "--owner-token", owner_token,
            "--mode", mode,
            "--transfer-scope", scope,
        ]
        if mode == "consumer" and not payload.get("dry_run"):
            cmd += ["--accept-token", token_path(paths, "B", to_email),
                    "--credentials", "credentials.json"]
        if payload.get("no_recursive"):
            cmd += ["--no-recursive"]
        if payload.get("no_notify"):
            cmd += ["--no-notify"]
        if payload.get("dry_run"):
            cmd += ["--dry-run"]
        log(f"::group::[{index}/{len(rows)}] Transfer {owner_email} -> {to_email}")
        code = run(cmd)
        log("::endgroup::")
        if code != 0:
            failures += 1
            log(f"::warning::Dòng {index} ({to_email}) trả về mã lỗi {code}.")
    return failures


def run_block(payload, paths):
    owner_token = token_path(paths, "A", payload["owner_email"])
    folders = payload.get("folders", [])
    if not folders:
        raise SystemExit("Payload block không có folder nào.")
    cmd = [sys.executable, "-u", "protect_videos.py", "block", "--token", owner_token]
    for fid in folders:
        cmd += ["--folder-id", fid]
    if payload.get("recursive"):
        cmd += ["--recursive"]
    if payload.get("unblock"):
        cmd += ["--unblock"]
    if payload.get("dry_run"):
        cmd += ["--dry-run"]
    action = "UNBLOCK" if payload.get("unblock") else "BLOCK"
    log(f"::group::{action} {len(folders)} folder(s)")
    code = run(cmd)
    log("::endgroup::")
    return 1 if code else 0


def main():
    raw_payload = os.environ.get("GHA_JOB_PAYLOAD", "").strip()
    if not raw_payload:
        raise SystemExit("Thiếu GHA_JOB_PAYLOAD.")
    payload = json.loads(raw_payload)
    kind = os.environ.get("GHA_JOB_KIND", "transfer").strip() or "transfer"

    bundle = load_bundle()
    paths = materialize_tokens(bundle)

    log(f"Owner Video Tool · GitHub Actions runner · kind={kind} dry_run={bool(payload.get('dry_run'))}")
    failures = run_transfer(payload, paths) if kind == "transfer" else run_block(payload, paths)

    if failures:
        log(f"Hoàn tất với {failures} lỗi.")
        return 1
    log("Hoàn tất, không lỗi.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
