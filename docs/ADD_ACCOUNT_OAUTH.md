# Thêm tài khoản bằng Google OAuth (không cần push code)

Trước đây mỗi lần thêm account phải login local → export → dán secret/env tay → redeploy.
Giờ token nằm trong **một GitHub Gist dùng chung**; web app trên Vercel ghi vào gist sau
khi bạn login Google, còn GitHub Actions đọc từ gist lúc chạy job. Thêm account =
mở web → **Quản lý tài khoản → Kết nối** → login Google → xong.

## Luồng hoạt động

```
Web (Vercel)  ──/api/oauth/start──▶ Google consent ──▶ /api/oauth/callback
                                                              │ ghi token
                                                              ▼
                                                     GitHub Gist (accounts.json)
                                                       ▲                    ▲
                                          Vercel đọc ──┘                    └── GitHub Actions đọc
```

File `accounts.json` trong gist:

```json
{
  "version": 1,
  "active_a": "owner@gmail.com",
  "A": [{ "email": "owner@gmail.com", "display_name": "...", "token_b64": "<base64 token json>" }],
  "B": [{ "email": "receiver@gmail.com", "display_name": "...", "token_b64": "..." }]
}
```

## Setup 1 lần

### 1. Google OAuth client loại "Web application"
- Google Cloud Console → APIs & Services → Credentials → **Create credentials → OAuth client ID**.
- Application type: **Web application**.
- **Authorized redirect URIs**: `https://<domain-vercel>/api/oauth/callback`
  (thêm cả domain preview nếu cần, mỗi domain một dòng).
- Bảo đảm OAuth consent screen đã bật scope `.../auth/drive`. Nếu app còn ở chế độ
  "Testing", thêm các email A/B vào danh sách **Test users**.
- Lưu lại **Client ID** và **Client secret**.

### 2. GitHub Gist + PAT
- Tạo **classic Personal Access Token** với scope **`gist`**
  (token fine-grained hiện chưa hỗ trợ gist). Nếu token này cũng dùng để dispatch
  workflow thì thêm scope `repo`/`workflow` như trước.
- Tạo một **secret gist** mới (https://gist.github.com) với 1 file tên `accounts.json`
  nội dung khởi tạo: `{ "version": 1, "active_a": "", "A": [], "B": [] }`.
- Copy **gist id** (đoạn hash trong URL gist).

### 3. Biến môi trường trên Vercel
Project Settings → Environment Variables (Production + Preview), rồi **Redeploy**:

| Biến | Giá trị |
|------|---------|
| `GOOGLE_WEB_CLIENT_ID` | Client ID ở bước 1 |
| `GOOGLE_WEB_CLIENT_SECRET` | Client secret ở bước 1 |
| `GH_API_TOKEN` | PAT có scope `gist` |
| `OWNER_TOOL_GIST_ID` | gist id ở bước 2 |

> `OWNER_TOOL_AUTH_SECRET` (đã có sẵn) được tái dùng để ký state OAuth — giữ nguyên.
> Có thể đặt cả `GOOGLE_WEB_CREDENTIALS_JSON` (dán nguyên file client_secret JSON tải về)
> thay cho 2 biến ID/SECRET nếu thích.

### 4. Secret trên GitHub repo (cho Actions)
Settings → Secrets and variables → Actions → New repository secret:

| Secret | Giá trị |
|--------|---------|
| `GH_API_TOKEN` | cùng PAT scope `gist` |
| `OWNER_TOOL_GIST_ID` | cùng gist id |

Secret cũ `OWNER_TOOL_ACCOUNTS_JSON_B64` vẫn để lại làm fallback; khi gist đã có
account thì nó không còn được dùng.

## Dùng hằng ngày
1. Mở web app → **Quản lý tài khoản**.
2. Bấm **Kết nối** ở Account A hoặc B → cửa sổ Google mở ra → chọn tài khoản → đồng ý.
3. Cửa sổ tự đóng, danh sách account cập nhật. Token đã nằm trong gist — Vercel và
   GitHub Actions dùng được ngay, **không cần push code hay sửa secret**.

## Lưu ý
- Google chỉ trả `refresh_token` ở lần consent đầu. Code đã ép `prompt=consent` nên
  luôn lấy được; nếu vẫn thiếu, gỡ quyền tại https://myaccount.google.com/permissions
  rồi kết nối lại.
- Token trong gist là bí mật — luôn dùng **secret gist** (không public) và PAT riêng,
  thu hồi được khi cần.
- Bản local `web_server.py` vẫn dùng luồng loopback cũ và lưu token ra file local;
  nó không tự đẩy lên gist. Trên production hãy thêm account qua web Vercel.
