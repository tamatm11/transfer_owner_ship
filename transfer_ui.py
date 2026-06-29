"""Desktop UI for the Drive video tools — no command line needed.

Two tabs:
  * Transfer ownership  -> runs auto_transfer_videos.py for up to 4 account batches
  * Block download      -> runs protect_videos.py block (copyRequiresWriterPermission)

Paste folder URLs into the left cells, choose the receiving account on the
right, and press Run. Output streams live into the log panel.

Run:  python transfer_ui.py
"""

from __future__ import annotations

import queue
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from b_account_store import (
    BAccount,
    import_token_file,
    load_registry,
    managed_token_path,
    save_registry,
    save_token_json,
    upsert_account,
)
from transfer_ownership import SCOPES, load_credentials

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TOOL_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOL_DIR  # standalone: token.json/credentials.json live beside the scripts
TRANSFER_SCRIPT = TOOL_DIR / "auto_transfer_videos.py"
PROTECT_SCRIPT = TOOL_DIR / "protect_videos.py"
B_ACCOUNT_REGISTRY = TOOL_DIR / "account_b_accounts.json"
B_ACCOUNT_TOKEN_DIR = TOOL_DIR / "account_b_tokens"
DEFAULT_CREDENTIALS_FILE = REPO_ROOT / "credentials.json"
DEFAULT_OWNER_TOKEN_FILE = REPO_ROOT / "token.json"
DEFAULT_TRANSFER_BATCHES = [
    {
        "folders": [
            "https://drive.google.com/drive/folders/1vs7Jm7Oze6-WWgD635XC_fb5Z32-Z-6M?usp=drive_link",
            "https://drive.google.com/drive/folders/1zTbJ6kUqEhUQLhQxzjTl-jHST4t5vaFv?usp=drive_link",
        ],
        "email": "onthidaihoc.otdh@gmail.com",
    },
    {
        "folders": [
            "https://drive.google.com/drive/folders/1rFOkkXnFHurXaywI5aoSHNwIiBOXgNzx?usp=drive_link",
            "https://drive.google.com/drive/folders/13sSyH8lu3kwe_U39t868OgBrPkZprJan?usp=drive_link",
        ],
        "email": "hoctapdautruong@gmail.com",
    },
    {
        "folders": [],
        "email": "onthidgnl.bachkhoa@gmail.com",
    },
]

# Reuse the URL parser so the Block tab can accept the same URL list and turn
# it into the repeated --folder-id arguments protect_videos.py expects.
try:
    from auto_transfer_videos import parse_folder_list
except Exception:  # pragma: no cover - import guard for friendlier UI error
    parse_folder_list = None  # type: ignore[assignment]


class DriveToolUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("Drive Video Tools — Transfer & Block")
        root.geometry("940x760")
        root.minsize(820, 680)

        self.proc: subprocess.Popen[str] | None = None
        self.running = False
        self.stop_requested = False
        self.log_queue: queue.Queue[object] = queue.Queue()
        self.oauth_in_progress = False
        saved_accounts, self.active_b_email = load_registry(B_ACCOUNT_REGISTRY)
        saved_accounts, registry_changed = self._normalize_saved_b_accounts(
            saved_accounts
        )
        self.b_accounts = {account.email: account for account in saved_accounts}
        if registry_changed:
            self._save_b_accounts()

        self._build_widgets()
        self.root.after(100, self._drain_log)

    # ------------------------------------------------------------------ #
    # layout
    # ------------------------------------------------------------------ #

    def _build_widgets(self) -> None:
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="x", padx=12, pady=(12, 6))

        self.transfer_vars = self._build_transfer_tab(notebook)
        self.block_vars = self._build_block_tab(notebook)

        # Shared controls.
        btn_row = ttk.Frame(self.root)
        btn_row.pack(fill="x", padx=12, pady=4)
        self.run_btn = ttk.Button(btn_row, text="Run", command=self._on_run)
        self.run_btn.pack(side="left")
        self.stop_btn = ttk.Button(
            btn_row, text="Stop", command=self._on_stop, state="disabled"
        )
        self.stop_btn.pack(side="left", padx=(8, 0))
        ttk.Button(btn_row, text="Clear log", command=self._clear_log).pack(
            side="right"
        )

        self.notebook = notebook

        # Log panel.
        log_frame = ttk.LabelFrame(self.root, text="Log")
        log_frame.pack(fill="both", expand=True, padx=12, pady=(6, 12))
        self.log = tk.Text(log_frame, height=14, wrap="word", state="disabled")
        self.log.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(log_frame, command=self.log.yview)
        scrollbar.pack(side="right", fill="y")
        self.log.configure(yscrollcommand=scrollbar.set)

    def _build_transfer_tab(self, notebook: ttk.Notebook) -> dict:
        tab = ttk.Frame(notebook)
        notebook.add(tab, text="Transfer ownership (A → B)")
        vars: dict = {}
        # Account callbacks can run while this tab is still being constructed.
        self.transfer_vars = vars

        ttk.Label(
            tab,
            text=(
                "Mỗi dòng: bên trái nhập folder URL/ID cần chuyển owner "
                "(nhiều link ngăn cách bằng dấu phẩy), bên phải chọn account nhận owner. "
                "Dòng trống sẽ được bỏ qua."
            ),
            wraplength=880,
            justify="left",
        ).pack(anchor="w", padx=10, pady=(10, 6))

        batch_frame = ttk.LabelFrame(tab, text="Danh sách chuyển ownership")
        batch_frame.pack(fill="x", padx=10, pady=(0, 8))
        batch_frame.columnconfigure(0, weight=1)
        batch_frame.columnconfigure(1, weight=0)

        ttk.Label(batch_frame, text="Folder URLs / IDs").grid(
            row=0, column=0, sticky="w", padx=(8, 6), pady=(6, 2)
        )
        ttk.Label(batch_frame, text="Account nhận owner").grid(
            row=0, column=1, sticky="w", padx=6, pady=(6, 2)
        )
        vars["batch_rows"] = []
        for index in range(4):
            folder_text = tk.Text(batch_frame, height=3, wrap="word")
            folder_text.grid(
                row=index + 1,
                column=0,
                sticky="ew",
                padx=(8, 6),
                pady=(0, 6),
            )
            account_var = tk.StringVar()
            account_combo = ttk.Combobox(
                batch_frame,
                textvariable=account_var,
                state="readonly",
                width=34,
            )
            account_combo.grid(
                row=index + 1,
                column=1,
                sticky="ew",
                padx=6,
                pady=(0, 6),
            )
            ttk.Button(
                batch_frame,
                text="Xóa",
                width=6,
                command=lambda row=index: self._clear_transfer_row(row),
            ).grid(row=index + 1, column=2, padx=(0, 8), pady=(0, 6))
            vars["batch_rows"].append(
                {
                    "folders_text": folder_text,
                    "account": account_var,
                    "account_combo": account_combo,
                }
            )

        manage_frame = ttk.LabelFrame(tab, text="Quản lý account nhận owner")
        manage_frame.pack(fill="x", padx=10, pady=(0, 8))
        account_row = ttk.Frame(manage_frame)
        account_row.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Label(account_row, text="Account đã lưu:").pack(side="left")
        vars["saved_account"] = tk.StringVar()
        self.b_account_combo = ttk.Combobox(
            account_row,
            textvariable=vars["saved_account"],
            state="readonly",
            width=34,
        )
        self.b_account_combo.pack(side="left", fill="x", expand=True, padx=(6, 0))
        ttk.Button(
            account_row,
            text="Đưa vào dòng trống",
            command=self._activate_selected_b_account,
        ).pack(side="left", padx=(6, 0))
        ttk.Button(
            account_row,
            text="Xóa 4 dòng",
            command=self._clear_transfer_rows,
        ).pack(side="left", padx=(6, 0))

        oauth_row = ttk.Frame(manage_frame)
        oauth_row.pack(fill="x", padx=8, pady=(0, 8))
        vars["credentials_file"] = tk.StringVar(value=str(DEFAULT_CREDENTIALS_FILE))
        self.add_b_account_btn = ttk.Button(
            oauth_row,
            text="Thêm tài khoản B",
            command=self._add_b_account,
        )
        self.add_b_account_btn.pack(side="left")
        ttk.Button(
            oauth_row,
            text="Chọn credentials.json",
            command=lambda: self._pick_file(vars["credentials_file"]),
        ).pack(side="left", padx=(6, 0))
        ttk.Button(
            account_row,
            text="Import token có sẵn",
            command=self._import_b_accounts,
        ).pack(side="left", padx=(6, 0))
        vars["oauth_status"] = tk.StringVar(
            value=f"OAuth: {DEFAULT_CREDENTIALS_FILE.name}"
        )
        ttk.Label(oauth_row, textvariable=vars["oauth_status"]).pack(
            side="left", padx=(10, 0)
        )

        vars["owner_token"] = self._file_field(
            tab,
            "Account A token (--owner-token):",
            default=str(DEFAULT_OWNER_TOKEN_FILE),
        )

        self._refresh_b_account_choices()
        self._prefill_batch_accounts()
        if self.active_b_email:
            vars["saved_account"].set(self.active_b_email)

        opts = ttk.Frame(tab)
        opts.pack(fill="x", padx=10, pady=(4, 2))
        ttk.Label(opts, text="Mode:").pack(side="left")
        vars["mode"] = tk.StringVar(value="consumer")
        ttk.Combobox(
            opts,
            textvariable=vars["mode"],
            values=["consumer", "workspace"],
            state="readonly",
            width=12,
        ).pack(side="left", padx=(6, 16))

        ttk.Label(opts, text="Phạm vi chuyển:").pack(side="left")
        vars["transfer_scope"] = tk.StringVar(value="videos")
        ttk.Combobox(
            opts,
            textvariable=vars["transfer_scope"],
            values=("videos", "folders", "all"),
            state="readonly",
            width=10,
        ).pack(side="left", padx=(6, 0))

        flags = ttk.Frame(tab)
        flags.pack(fill="x", padx=10, pady=(2, 2))
        vars["recursive"] = tk.BooleanVar(value=True)
        ttk.Checkbutton(flags, text="Recursive", variable=vars["recursive"]).pack(
            side="left", padx=4
        )
        vars["no_notify"] = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            flags,
            text="No email notify (workspace only)",
            variable=vars["no_notify"],
        ).pack(side="left", padx=4)
        vars["dry_run"] = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            flags, text="Dry run (preview)", variable=vars["dry_run"]
        ).pack(side="left", padx=4)

        return vars

    def _refresh_b_account_choices(self) -> None:
        if not hasattr(self, "b_account_combo"):
            return
        emails = sorted(self.b_accounts, key=str.casefold)
        self.b_account_combo.configure(values=emails)
        for row in self.transfer_vars.get("batch_rows", []):
            row["account_combo"].configure(values=emails)

    def _prefill_batch_accounts(self) -> None:
        rows = self.transfer_vars.get("batch_rows", [])
        for row, batch in zip(rows, DEFAULT_TRANSFER_BATCHES):
            folders = batch["folders"]
            email = batch["email"]
            if folders and not row["folders_text"].get("1.0", "end").strip():
                row["folders_text"].insert("1.0", ", ".join(folders))
            if email in self.b_accounts and not row["account"].get().strip():
                row["account"].set(email)

    def _clear_transfer_row(self, index: int) -> None:
        rows = self.transfer_vars.get("batch_rows", [])
        if index < 0 or index >= len(rows):
            return
        row = rows[index]
        row["folders_text"].delete("1.0", "end")
        row["account"].set("")

    def _clear_transfer_rows(self) -> None:
        for index in range(len(self.transfer_vars.get("batch_rows", []))):
            self._clear_transfer_row(index)

    def _fill_next_empty_account(self, email: str) -> int | None:
        for index, row in enumerate(self.transfer_vars.get("batch_rows", []), start=1):
            if not row["account"].get().strip():
                row["account"].set(email)
                return index
        return None

    def _save_b_accounts(self) -> None:
        save_registry(
            B_ACCOUNT_REGISTRY,
            list(self.b_accounts.values()),
            self.active_b_email,
        )

    @staticmethod
    def _path_from_ui(raw_path: str, *, default: Path) -> Path:
        path = Path(raw_path.strip() or str(default)).expanduser()
        if not path.is_absolute():
            path = TOOL_DIR / path
        return path

    @staticmethod
    def _same_path(left: Path, right: Path) -> bool:
        try:
            return left.resolve() == right.resolve()
        except OSError:
            return left.absolute() == right.absolute()

    def _normalize_saved_b_accounts(
        self, accounts: list[BAccount]
    ) -> tuple[list[BAccount], bool]:
        normalized: list[BAccount] = []
        changed = False
        for account in accounts:
            current_path = self._path_from_ui(
                account.token_path,
                default=managed_token_path(B_ACCOUNT_TOKEN_DIR, account.email),
            )
            local_path = managed_token_path(B_ACCOUNT_TOKEN_DIR, account.email)
            token_path = current_path
            if local_path.is_file() and not self._same_path(current_path, local_path):
                token_path = local_path
                changed = True
            elif not current_path.is_file() and local_path.is_file():
                token_path = local_path
                changed = True

            normalized.append(
                BAccount(
                    email=account.email,
                    token_path=str(token_path),
                    display_name=account.display_name,
                )
            )
        return normalized, changed

    def _activate_selected_b_account(self, *, show_message: bool = True) -> None:
        email = self.transfer_vars["saved_account"].get().strip()
        account = self.b_accounts.get(email)
        if account is None:
            if show_message:
                messagebox.showinfo(
                    "Account B", "Import or select an account B first."
                )
            return

        self.active_b_email = account.email
        self._save_b_accounts()
        filled_row = self._fill_next_empty_account(account.email)
        if show_message:
            label = account.display_name or account.email
            if filled_row is None:
                message = f"Đã chọn account:\n{label}\n{account.email}"
            else:
                message = (
                    f"Đã đưa account vào dòng {filled_row}:\n"
                    f"{label}\n{account.email}"
                )
            messagebox.showinfo("Account B", message)

    @staticmethod
    def _register_chrome_browser() -> str | None:
        candidates = (
            Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
            Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
            Path.home() / "AppData/Local/Google/Chrome/Application/chrome.exe",
        )
        chrome_path = next((path for path in candidates if path.is_file()), None)
        if chrome_path is None:
            return None
        browser_name = "drive-tool-chrome"
        webbrowser.register(
            browser_name,
            None,
            webbrowser.BackgroundBrowser(str(chrome_path)),
        )
        return browser_name

    def _add_b_account(self) -> None:
        if self.oauth_in_progress:
            messagebox.showinfo(
                "OAuth", "Đang chờ hoàn tất đăng nhập tài khoản B trên Chrome."
            )
            return

        credentials_path = self._path_from_ui(
            self.transfer_vars["credentials_file"].get(),
            default=DEFAULT_CREDENTIALS_FILE,
        )
        if not credentials_path.is_file():
            messagebox.showerror(
                "Thiếu credentials.json",
                f"Không tìm thấy file OAuth credentials:\n{credentials_path}",
            )
            return

        self.oauth_in_progress = True
        self.add_b_account_btn.configure(state="disabled")
        self.transfer_vars["oauth_status"].set("Đang mở Chrome để đăng nhập...")
        self._append_log(
            f"\n[OAuth] Mở Chrome để thêm tài khoản B bằng {credentials_path}\n"
        )
        threading.Thread(
            target=self._oauth_b_account_worker,
            args=(credentials_path,),
            daemon=True,
        ).start()

    def _oauth_b_account_worker(self, credentials_path: Path) -> None:
        try:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(credentials_path), scopes=SCOPES
            )
            browser_name = self._register_chrome_browser()
            try:
                creds = flow.run_local_server(
                    host="localhost",
                    port=0,
                    open_browser=True,
                    browser=browser_name,
                    # google-auth-oauthlib 1.2.x raises an internal
                    # NoneType.replace error when this timeout expires before
                    # Google redirects back. Wait until login completes.
                    timeout_seconds=None,
                    authorization_prompt_message=None,
                    success_message=(
                        "Đăng nhập thành công. Bạn có thể đóng tab này và quay lại tool."
                    ),
                    access_type="offline",
                    prompt="select_account consent",
                )
            except AttributeError as exc:
                if "'NoneType' object has no attribute 'replace'" not in str(exc):
                    raise
                raise RuntimeError(
                    "Không nhận được phản hồi đăng nhập từ Chrome. "
                    "Hãy bấm Thêm tài khoản B và hoàn tất đăng nhập trên tab Google."
                ) from exc
            if not creds.refresh_token:
                raise RuntimeError(
                    "Google did not return a refresh token, so this token would "
                    "expire quickly. Remove this app from Google Account > "
                    "Security > Third-party access, then add the account again."
                )

            service = build(
                "drive", "v3", credentials=creds, cache_discovery=False
            )
            user = service.about().get(
                fields="user(displayName,emailAddress)"
            ).execute().get("user", {})
            email = str(user.get("emailAddress", "")).strip()
            if not email:
                raise RuntimeError("Google Drive không trả về email tài khoản")

            token_path = save_token_json(
                B_ACCOUNT_TOKEN_DIR, email, creds.to_json()
            )
            account = BAccount(
                email=email,
                display_name=str(user.get("displayName", "")).strip(),
                token_path=str(token_path),
            )
            self.log_queue.put(("__OAUTH_DONE__", account, ""))
        except Exception as exc:
            self.log_queue.put(("__OAUTH_DONE__", None, str(exc)))

    def _finish_oauth(self, account: BAccount | None, error: str) -> None:
        self.oauth_in_progress = False
        self.add_b_account_btn.configure(state="normal")
        if error or account is None:
            self.transfer_vars["oauth_status"].set("Đăng nhập chưa hoàn tất")
            self._append_log(f"[OAuth ERR] {error}\n")
            messagebox.showerror("Thêm tài khoản B thất bại", error)
            return

        current = upsert_account(list(self.b_accounts.values()), account)
        self.b_accounts = {item.email: item for item in current}
        self.active_b_email = account.email
        self._save_b_accounts()
        self._refresh_b_account_choices()
        self.transfer_vars["saved_account"].set(account.email)
        self._activate_selected_b_account(show_message=False)
        self.transfer_vars["oauth_status"].set(f"Đã lưu: {account.email}")
        self._append_log(f"[OAuth OK] Đã lưu tài khoản B: {account.email}\n")
        messagebox.showinfo(
            "Đã thêm tài khoản B",
            f"Đã đăng nhập và lưu token cho:\n{account.email}",
        )

    def _import_b_accounts(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Import account B token JSON files",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
        )
        if not paths:
            return

        imported: list[BAccount] = []
        errors: list[str] = []
        for raw_path in paths:
            source = Path(raw_path)
            try:
                creds = load_credentials(str(source))
                service = build(
                    "drive", "v3", credentials=creds, cache_discovery=False
                )
                user = service.about().get(
                    fields="user(displayName,emailAddress)"
                ).execute().get("user", {})
                email = str(user.get("emailAddress", "")).strip()
                if not email:
                    raise RuntimeError("Google Drive did not return an account email")

                token_path = import_token_file(source, B_ACCOUNT_TOKEN_DIR, email)
                account = BAccount(
                    email=email,
                    display_name=str(user.get("displayName", "")).strip(),
                    token_path=str(token_path),
                )
                current = upsert_account(list(self.b_accounts.values()), account)
                self.b_accounts = {item.email: item for item in current}
                imported.append(account)
            except Exception as exc:
                errors.append(f"{source.name}: {exc}")

        if imported:
            selected = imported[-1]
            self.active_b_email = selected.email
            self._save_b_accounts()
            self._refresh_b_account_choices()
            self.transfer_vars["saved_account"].set(selected.email)
            self._activate_selected_b_account(show_message=False)

        summary = f"Imported {len(imported)} account B token(s)."
        if errors:
            summary += "\n\nFailed:\n" + "\n".join(errors)
            messagebox.showwarning("Import account B", summary)
        else:
            messagebox.showinfo("Import account B", summary)

    def _build_block_tab(self, notebook: ttk.Notebook) -> dict:
        tab = ttk.Frame(notebook)
        notebook.add(tab, text="Block download")
        vars: dict = {}

        self._folders_field(tab, vars)

        vars["token"] = self._file_field(
            tab,
            "Owner token (--token):",
            default=str(DEFAULT_OWNER_TOKEN_FILE),
        )

        opts = ttk.Frame(tab)
        opts.pack(fill="x", padx=10, pady=(4, 2))
        vars["recursive"] = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Recursive", variable=vars["recursive"]).pack(
            side="left", padx=4
        )
        vars["unblock"] = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            opts, text="Unblock (re-allow download)", variable=vars["unblock"]
        ).pack(side="left", padx=4)
        vars["dry_run"] = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            opts, text="Dry run (preview)", variable=vars["dry_run"]
        ).pack(side="left", padx=4)

        return vars

    # ------------------------------------------------------------------ #
    # field helpers
    # ------------------------------------------------------------------ #

    def _folders_field(self, parent: ttk.Frame, vars: dict) -> None:
        ttk.Label(
            parent, text="Folder URLs (comma-separated):"
        ).pack(anchor="w", padx=10, pady=(10, 2))
        text = tk.Text(parent, height=4, wrap="word")
        text.pack(fill="x", padx=10)
        vars["folders_text"] = text

    def _entry_field(self, parent: ttk.Frame, label: str) -> tk.StringVar:
        ttk.Label(parent, text=label).pack(anchor="w", padx=10, pady=(8, 2))
        var = tk.StringVar()
        ttk.Entry(parent, textvariable=var).pack(fill="x", padx=10)
        return var

    def _file_field(
        self, parent: ttk.Frame, label: str, *, default: str = ""
    ) -> tk.StringVar:
        ttk.Label(parent, text=label).pack(anchor="w", padx=10, pady=(8, 2))
        row = ttk.Frame(parent)
        row.pack(fill="x", padx=10)
        var = tk.StringVar(value=default)
        ttk.Entry(row, textvariable=var).pack(
            side="left", fill="x", expand=True
        )
        ttk.Button(
            row, text="Browse", command=lambda: self._pick_file(var)
        ).pack(side="left", padx=(6, 0))
        return var

    def _pick_file(self, var: tk.StringVar) -> None:
        path = filedialog.askopenfilename(
            title="Select token JSON",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
        )
        if path:
            var.set(path)

    # ------------------------------------------------------------------ #
    # command building
    # ------------------------------------------------------------------ #

    def _build_commands(self) -> list[tuple[str, list[str]]] | None:
        tab_index = self.notebook.index(self.notebook.select())
        if tab_index == 0:
            return self._transfer_commands()
        cmd = self._block_command()
        if cmd is None:
            return None
        return [("Block download", cmd)]

    def _transfer_commands(self) -> list[tuple[str, list[str]]] | None:
        v = self.transfer_vars
        if parse_folder_list is None:
            messagebox.showerror(
                "Error", "Could not load URL parser (auto_transfer_videos.py)."
            )
            return None

        commands: list[tuple[str, list[str]]] = []
        for index, row in enumerate(v.get("batch_rows", []), start=1):
            folders = row["folders_text"].get("1.0", "end").strip()
            email = row["account"].get().strip()
            if not folders:
                continue
            if not email:
                messagebox.showerror(
                    "Thiếu account",
                    f"Dòng {index} có folder nhưng chưa chọn account nhận owner.",
                )
                return None

            account = self.b_accounts.get(email)
            if account is None:
                messagebox.showerror(
                    "Account chưa lưu",
                    (
                        f"Dòng {index} đang chọn account chưa có token:\n{email}\n\n"
                        "Hãy bấm Thêm tài khoản B hoặc Import token có sẵn trước."
                    ),
                )
                return None

            try:
                folder_ids = parse_folder_list(folders)
            except ValueError as exc:
                messagebox.showerror("Folder không hợp lệ", f"Dòng {index}: {exc}")
                return None

            owner_token = self._path_from_ui(
                v["owner_token"].get(),
                default=DEFAULT_OWNER_TOKEN_FILE,
            )
            cmd = [
                sys.executable,
                "-u",
                str(TRANSFER_SCRIPT),
                "--folders",
                ",".join(folder_ids),
                "--to-email",
                account.email,
                "--owner-token",
                str(owner_token),
                "--mode",
                v["mode"].get(),
                "--transfer-scope",
                v["transfer_scope"].get(),
            ]
            if v["mode"].get() == "consumer" and account.token_path:
                accept_token = self._path_from_ui(
                    account.token_path,
                    default=managed_token_path(B_ACCOUNT_TOKEN_DIR, account.email),
                )
                if not accept_token.is_file():
                    messagebox.showerror(
                        "Thiếu token account B",
                        (
                            f"Dòng {index} chọn account:\n{account.email}\n\n"
                            f"Không tìm thấy token B:\n{accept_token}\n\n"
                            "Hãy bấm Thêm tài khoản B hoặc Import token có sẵn."
                        ),
                    )
                    return None
                cmd += [
                    "--accept-token",
                    str(accept_token),
                    "--credentials",
                    str(
                        self._path_from_ui(
                            v["credentials_file"].get(),
                            default=DEFAULT_CREDENTIALS_FILE,
                        )
                    ),
                    "--reauth-accept-token",
                ]
            if not v["recursive"].get():
                cmd.append("--no-recursive")
            if v["no_notify"].get():
                cmd.append("--no-notify")
            if v["dry_run"].get():
                cmd.append("--dry-run")

            label = f"Dòng {index}: {len(folder_ids)} folder -> {account.email}"
            commands.append((label, cmd))

        if not commands:
            messagebox.showerror(
                "Missing",
                "Hãy nhập ít nhất một dòng folder URL/ID cần chuyển owner.",
            )
            return None
        return commands

    def _block_command(self) -> list[str] | None:
        v = self.block_vars
        folders = v["folders_text"].get("1.0", "end").strip()
        if not folders:
            messagebox.showerror("Missing", "Please paste at least one folder URL.")
            return None
        if parse_folder_list is None:
            messagebox.showerror(
                "Error", "Could not load URL parser (auto_transfer_videos.py)."
            )
            return None
        try:
            folder_ids = parse_folder_list(folders)
        except ValueError as exc:
            messagebox.showerror("Bad folders", str(exc))
            return None

        cmd = [
            sys.executable,
            "-u",
            str(PROTECT_SCRIPT),
            "block",
            "--token",
            str(self._path_from_ui(v["token"].get(), default=DEFAULT_OWNER_TOKEN_FILE)),
        ]
        for fid in folder_ids:
            cmd += ["--folder-id", fid]
        if v["recursive"].get():
            cmd.append("--recursive")
        if v["unblock"].get():
            cmd.append("--unblock")
        if v["dry_run"].get():
            cmd.append("--dry-run")
        return cmd

    # ------------------------------------------------------------------ #
    # run / stop / log
    # ------------------------------------------------------------------ #

    def _on_run(self) -> None:
        if self.running:
            messagebox.showinfo("Busy", "A task is already running.")
            return
        commands = self._build_commands()
        if commands is None:
            return

        self.running = True
        self.stop_requested = False
        self._append_log(f"\n[plan] {len(commands)} batch(es) queued.\n")
        self.run_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")

        thread = threading.Thread(
            target=self._run_worker,
            args=(commands,),
            daemon=True,
        )
        thread.start()

    def _run_worker(self, commands: list[tuple[str, list[str]]]) -> None:
        failed_batches = 0
        for index, (label, cmd) in enumerate(commands, start=1):
            if self.stop_requested:
                self.log_queue.put("\n[stopped before next batch]\n")
                break

            self.log_queue.put(f"\n=== Batch {index}/{len(commands)}: {label} ===\n")
            self.log_queue.put("$ " + subprocess.list2cmdline(cmd) + "\n")
            try:
                self.proc = subprocess.Popen(
                    cmd,
                    cwd=str(REPO_ROOT),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                )
            except Exception as exc:  # pragma: no cover - surfaced in UI
                failed_batches += 1
                self.log_queue.put(f"[ERR] failed to start: {exc}\n")
                continue

            assert self.proc.stdout is not None
            for line in self.proc.stdout:
                self.log_queue.put(line)
            self.proc.wait()
            return_code = self.proc.returncode
            self.proc = None
            if return_code:
                failed_batches += 1
            self.log_queue.put(f"\n[exit code {return_code}]\n")

        if failed_batches:
            self.log_queue.put(f"\n[done] {failed_batches} batch(es) failed.\n")
        else:
            self.log_queue.put("\n[done] all queued batches finished.\n")
        self.log_queue.put("__DONE__")

    def _on_stop(self) -> None:
        self.stop_requested = True
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            self._append_log("\n[stopping…]\n")
        elif self.running:
            self._append_log("\n[stop requested]\n")

    def _drain_log(self) -> None:
        try:
            while True:
                item = self.log_queue.get_nowait()
                if isinstance(item, tuple) and item[0] == "__OAUTH_DONE__":
                    self._finish_oauth(item[1], item[2])
                    continue
                if item == "__DONE__":
                    self.proc = None
                    self.running = False
                    self.stop_requested = False
                    self.run_btn.configure(state="normal")
                    self.stop_btn.configure(state="disabled")
                else:
                    self._append_log(str(item))
        except queue.Empty:
            pass
        self.root.after(100, self._drain_log)

    def _append_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")


def main() -> int:
    root = tk.Tk()
    DriveToolUI(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
