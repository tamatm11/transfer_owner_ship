# Plan: Chạy job ngầm bằng GitHub Actions

> Cập nhật: runtime hiện tại đọc account từ encrypted Upstash/Vercel KV
> (`KV_REST_API_URL`, `KV_REST_API_READ_ONLY_TOKEN`, `OWNER_TOOL_STORE_KEY`).
> Các đoạn bên dưới nhắc `OWNER_TOOL_ACCOUNTS_JSON_B64` chỉ còn là ghi chú legacy/fallback;
> setup mới xem `docs/ADD_ACCOUNT_OAUTH.md`.

Mục tiêu: bật tool trên web (Vercel) → out ra → GitHub Actions chạy transfer/block tới
khi xong (tới 6h/job), độc lập với trình duyệt. Tái dùng nguyên CLI Python hiện có.

---

## 0. Kiến trúc tổng thể

```
[Web Vercel]  ── chọn A/B, URL, scope, dry-run ──► POST /api/jobs/transfer
     ▲                                                    │
     │ poll GET /api/jobs/:id                             │ validate nhanh (<60s)
     │                                                    ▼
[Vercel API] ──────── workflow_dispatch (REST) ──► [GitHub Actions runner]
     ▲                  inputs: payload JSON,            │
     │                  job_id, kind                     │ giải mã token từ Secret
     │ GET runs?event=workflow_dispatch                  │ chạy auto_transfer_videos.py
     └────────────── trạng thái run ◄───────────────────┘ (lặp từng dòng A→B)
```

- **Chọn lựa** vẫn 100% trên web (TransferForm/BlockForm/Shell không đổi UX).
- Web gom toàn bộ lựa chọn thành **1 khối JSON** → gửi qua **1 input duy nhất**
  (né giới hạn 10 input của `workflow_dispatch`).
- **Token A/B không qua trình duyệt**: web chỉ gửi *email*; Action lấy token từ
  GitHub Secret (chính là `OWNER_TOOL_ACCOUNTS_JSON_B64` đã có sẵn).

---

## 1. Tạo repo private + đẩy code

Hiện thư mục **chưa phải git repo**. Các bước:

```powershell
cd E:\up-khoa-hoc\owner_video_tool
git init
git add .
git commit -m "Owner Video Tool: initial"
gh repo create owner-video-tool --private --source . --remote origin --push
```

**Quan trọng – KHÔNG commit secret.** Kiểm tra `.gitignore` đã chặn:
`.env.*`, `token.json`, `token_B.json`, `credentials.json`,
`account_a_accounts.json`, `account_b_accounts.json`,
`account_a_tokens/`, `account_b_tokens/`.

> Token Google (refresh token) chỉ tồn tại ở 2 nơi: máy local của anh và
> **GitHub Secrets** (mã hóa). Không bao giờ nằm trong source.

---

## 2. Cấu trúc GitHub Secrets

Tạo trong repo: **Settings → Secrets and variables → Actions → New secret**.

| Secret | Nội dung | Lấy từ đâu |
| --- | --- | --- |
| `OWNER_TOOL_ACCOUNTS_JSON_B64` | base64 của `{active_a, A:[{email,token_b64}], B:[...]}` | dòng `OWNER_TOOL_ACCOUNTS_JSON_B64=` trong `.env.local` |
| `CREDENTIALS_JSON_B64` | base64 của `credentials.json` (OAuth client) | `[Convert]::ToBase64String([IO.File]::ReadAllBytes("credentials.json"))` |

> Mỗi lần đổi/refresh token A hoặc B: chạy lại
> `npm run export:vercel-env -- --password "..."` rồi cập nhật secret
> `OWNER_TOOL_ACCOUNTS_JSON_B64` (đúng quy trình hiện tại, không phát sinh thêm).

**Lưu ý headless:** runner không có trình duyệt nên **không dùng**
`--reauth-accept-token`. Token B phải còn hạn. Nếu B hết hạn → job fail, anh chạy
lại export rồi cập nhật secret. Validation ở Vercel (mục 6) sẽ cảnh báo sớm.

---

## 3. Script runner Python (mới)

File `scripts/gha_run.py` — đọc payload JSON, giải mã token bundle ra file tạm,
lặp từng dòng và gọi CLI có sẵn. Stream log ra stdout (Actions tự lưu).

```python
# scripts/gha_run.py
import base64, json, os, subprocess, sys, tempfile, pathlib

def materialize_tokens(bundle):
    """Giải mã token bundle thành file, trả map email -> path."""
    root = pathlib.Path(tempfile.mkdtemp(prefix="gha_tokens_"))
    paths = {}
    for role in ("A", "B"):
        for acc in bundle.get(role, []):
            p = root / f"{role}_{acc['email'].replace('@','_at_')}.json"
            p.write_bytes(base64.b64decode(acc["token_b64"]))
            paths[(role, acc["email"])] = str(p)
    return paths

def run_transfer(payload, paths):
    owner_email = payload["owner_email"]
    owner_token = paths[("A", owner_email)]
    mode = payload.get("mode", "consumer")
    scope = payload.get("scope", "videos")
    failures = 0
    for row in payload["rows"]:
        to_email = row["receiver_email"]
        folders = ",".join(row["folders"])
        cmd = [sys.executable, "-u", "auto_transfer_videos.py",
               "--folders", folders, "--to-email", to_email,
               "--owner-token", owner_token,
               "--mode", mode, "--transfer-scope", scope]
        if mode == "consumer":
            cmd += ["--accept-token", paths[("B", to_email)],
                    "--credentials", "credentials.json"]
        if payload.get("no_recursive"): cmd += ["--no-recursive"]
        if payload.get("no_notify"):    cmd += ["--no-notify"]
        if payload.get("dry_run"):      cmd += ["--dry-run"]
        print(f"::group::Transfer {owner_email} -> {to_email}", flush=True)
        rc = subprocess.call(cmd)
        print("::endgroup::", flush=True)
        if rc != 0: failures += 1
    return failures

def run_block(payload, paths):
    owner_token = paths[("A", payload["owner_email"])]
    cmd = [sys.executable, "-u", "protect_videos.py", "block",
           "--token", owner_token]
    for fid in payload["folders"]:
        cmd += ["--folder-id", fid]
    if payload.get("recursive"): cmd += ["--recursive"]
    if payload.get("unblock"):   cmd += ["--unblock"]
    if payload.get("dry_run"):   cmd += ["--dry-run"]
    return subprocess.call(cmd)

def main():
    payload = json.loads(os.environ["GHA_JOB_PAYLOAD"])
    bundle = json.loads(base64.b64decode(os.environ["OWNER_TOOL_ACCOUNTS_JSON_B64"]))
    paths = materialize_tokens(bundle)
    kind = os.environ.get("GHA_JOB_KIND", "transfer")
    rc = run_transfer(payload, paths) if kind == "transfer" else run_block(payload, paths)
    sys.exit(1 if rc else 0)

if __name__ == "__main__":
    main()
```

> Lặp từng dòng đúng như `web_server.py` đang làm (mỗi dòng = 1 lần gọi CLI với
> `--to-email` riêng). Kiểm tra lại tên flag block trong `protect_videos.py`
> trước khi chốt (`--folder-id`, `--recursive`, `--unblock`).

---

## 4. Workflow YAML

File `.github/workflows/owner-tool.yml`:

```yaml
name: owner-tool-job
run-name: "owner-tool ${{ inputs.kind }} ${{ inputs.job_id }}"

on:
  workflow_dispatch:
    inputs:
      kind:    { description: transfer|block, required: true, type: string }
      job_id:  { description: id job, required: true, type: string }
      payload: { description: JSON payload, required: true, type: string }

concurrency:
  group: owner-tool-drive   # chạy tuần tự, tránh đụng quota Drive
  cancel-in-progress: false

jobs:
  run:
    runs-on: ubuntu-latest
    timeout-minutes: 350
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11" }
      - run: pip install -r requirements.txt
      - name: Restore credentials.json
        run: echo "$CREDS_B64" | base64 -d > credentials.json
        env:
          CREDS_B64: ${{ secrets.CREDENTIALS_JSON_B64 }}
      - name: Run job
        env:
          GHA_JOB_KIND: ${{ inputs.kind }}
          GHA_JOB_PAYLOAD: ${{ inputs.payload }}
          OWNER_TOOL_ACCOUNTS_JSON_B64: ${{ secrets.OWNER_TOOL_ACCOUNTS_JSON_B64 }}
        run: python -u scripts/gha_run.py
```

- `run-name` nhúng `job_id` → Vercel tìm run theo tên để map trạng thái.
- `concurrency` đảm bảo job Drive chạy tuần tự (giữ hành vi "1 job/lần" hiện tại).
  Muốn song song thì bỏ block này.
- `timeout-minutes: 350` ≈ gần 6h, dư cho batch 500+ video.

---

## 5. Sửa Vercel API — `api/[...path].js`

Thay `handleTransfer`/`handleBlock` từ "chạy đồng bộ trong request" sang
"validate nhanh + dispatch GitHub + trả job_id".

### 5a. ENV mới trên Vercel
```
GITHUB_REPO=<user>/owner-video-tool
GITHUB_WORKFLOW_FILE=owner-tool.yml
GITHUB_DISPATCH_TOKEN=<fine-grained PAT: Actions read+write trên repo này>
```

### 5b. Dispatch (POST /api/jobs/transfer | /block)
```js
async function dispatchWorkflow(kind, payload) {
  const job_id = crypto.randomUUID()
  const repo = process.env.GITHUB_REPO
  const wf = process.env.GITHUB_WORKFLOW_FILE
  const res = await fetch(
    `https://api.github.com/repos/${repo}/actions/workflows/${wf}/dispatches`,
    { method: 'POST',
      headers: { Authorization: `Bearer ${process.env.GITHUB_DISPATCH_TOKEN}`,
                 Accept: 'application/vnd.github+json' },
      body: JSON.stringify({ ref: 'main',
        inputs: { kind, job_id, payload: JSON.stringify(payload) } }) })
  if (res.status !== 204) throw new Error(`Dispatch fail: ${res.status} ${await res.text()}`)
  return { id: job_id, type: kind, status: 'queued', created: Date.now() }
}
```
> Trước khi dispatch: giữ **validation nhanh** (folder hợp lệ? account có token?
> email B đã đăng ký?) bằng code Drive sẵn có — vài folder check thừa sức trong 60s.
> Lỗi nhập liệu báo ngay, chỉ phần transfer nặng mới đẩy sang Actions.

### 5c. Trạng thái (GET /api/jobs/:id)
`workflow_dispatch` không trả run id, nên map qua `run-name`:
```js
async function jobStatus(id) {
  const repo = process.env.GITHUB_REPO
  const res = await fetch(
    `https://api.github.com/repos/${repo}/actions/runs?event=workflow_dispatch&per_page=30`,
    { headers: { Authorization: `Bearer ${process.env.GITHUB_DISPATCH_TOKEN}`,
                 Accept: 'application/vnd.github+json' } })
  const runs = (await res.json()).workflow_runs || []
  const run = runs.find(r => (r.name || '').includes(id))
  if (!run) return { id, status: 'queued' }
  // GitHub status: queued|in_progress|completed ; conclusion: success|failure|cancelled
  const map = { queued: 'queued', in_progress: 'running',
                completed: run.conclusion === 'success' ? 'completed'
                         : run.conclusion === 'cancelled' ? 'stopped' : 'failed' }
  return { id, status: map[run.status] || 'running',
           run_url: run.html_url, run_id: run.id }
}
```

### 5d. Stop (POST /api/jobs/:id/stop)
Tìm run theo `id` (như 5c) rồi gọi
`POST /repos/{repo}/actions/runs/{run_id}/cancel`.

---

## 6. Sửa frontend (tối thiểu)

Vòng poll trong `App.tsx` đã chạy theo `job.status` (`queued/running` → poll 750ms)
nên gần như **không phải đổi**. Chỉ bổ sung:

1. `api.ts`: giữ nguyên endpoint `/api/jobs/:id` (giờ trả thêm `run_url`, `run_id`).
2. `JobLog.tsx`: khi job đang chạy bằng Actions, hiện **link "Xem log trên GitHub"**
   (`run_url`) thay cho log live (v1). Trạng thái queued/running/completed/failed/stopped
   vẫn hiển thị như cũ.
3. (Tùy chọn) đổi poll interval lên 2–3s cho job Actions để tiết kiệm rate-limit
   GitHub API.

> **Log live từng dòng** là nâng cấp v2: workflow đẩy tiến độ về Vercel KV/Upstash,
> frontend đọc KV. Chưa cần cho fire-and-forget.

---

## 7. Bảo mật & vận hành

- **PAT fine-grained**: chỉ quyền `Actions: Read and write` trên đúng repo này.
  Lưu ở Vercel env `GITHUB_DISPATCH_TOKEN`, không ra browser.
- **Auth web** giữ nguyên: chỉ `tamatm6713@gmail.com` đăng nhập được mới gọi dispatch.
- **Redact log**: CLI/Action không in token (token chỉ là path file tạm trong runner).
- **Quota**: private repo ~2000 phút/tháng miễn phí. 1 job vài–vài chục phút →
  thoải mái. `concurrency` ngăn chạy chồng làm vỡ quota Drive.
- **Dọn token**: runner ghi token ra thư mục tạm của runner (bị hủy khi job xong).

---

## 8. Checklist triển khai (thứ tự)

Phần CODE đã hoàn thành (✅). Phần còn lại là thao tác thủ công của anh (☐).

- [x] **B4** ✅ `scripts/gha_run.py` — runner (đã khớp flag transfer + block).
- [x] **B5** ✅ `.github/workflows/owner-tool.yml` — workflow nhận JSON input.
- [x] **B7** ✅ `api/[...path].js` — dispatch + status + stop (tự bật khi có env GitHub).
- [x] **B9** ✅ `JobLog.tsx` + `types.ts` hiện link "Xem log trên GitHub"; poll nhẹ hơn (1.5s).
- [x] **B-extra** ✅ `npm run print:github-secrets` in sẵn giá trị secret để copy.
- [ ] **B1** `git init` + tạo repo private + push (kiểm tra `.gitignore`).
- [ ] **B2** Tạo PAT fine-grained (Actions: Read and write) cho repo.
- [ ] **B3** Thêm 2 GitHub Secrets (chạy `npm run print:github-secrets` để lấy giá trị):
      `OWNER_TOOL_ACCOUNTS_JSON_B64` (bắt buộc), `CREDENTIALS_JSON_B64` (tùy chọn).
- [ ] **B6** Test thủ công trên GitHub: tab Actions → owner-tool-job → Run workflow →
      `kind=transfer`, `job_id=test1`,
      `payload={"owner_email":"A@gmail.com","mode":"consumer","scope":"videos","dry_run":true,"rows":[{"receiver_email":"B@gmail.com","folders":["<FOLDER_ID>"]}]}`
      → xem log chạy đúng.
- [ ] **B8** Thêm Vercel env: `GITHUB_REPO=<user>/<repo>`, `GITHUB_DISPATCH_TOKEN=<PAT>`
      (và tùy chọn `GITHUB_WORKFLOW_FILE`, `GITHUB_REF`). **Có env này → web tự chạy chế độ dispatch.**
- [ ] **B10** Deploy Vercel → bấm chạy từ web với `dry_run` → xác nhận end-to-end.
- [ ] **B11** Chạy thật 1 batch nhỏ (5–10 video) → rồi mới batch lớn.

> Khi CHƯA set `GITHUB_REPO`/`GITHUB_DISPATCH_TOKEN`: web chạy y như cũ (đồng bộ trong
> request, hợp batch nhỏ). Khi ĐÃ set: tự chuyển sang fire-and-forget qua GitHub Actions.
> Không cần đổi code để bật/tắt.

---

## 9. Đánh đổi đã biết

| Ưu | Nhược |
| --- | --- |
| Miễn phí, không cần server always-on | Lần đầu phải tạo repo + lưu secret |
| Tái dùng nguyên CLI Python đang chạy ổn | Log live phải nâng cấp v2 (KV) |
| Fire-and-forget thật, tới 6h/job | Runner khởi động chậm ~20–40s |
| Chạy song song hoặc tuần tự tùy chọn | Token B hết hạn phải export lại secret |
| Có lưu lịch sử log từng run trên GitHub | Map run↔job qua run-name (1 lần setup) |
</content>
</invoke>
