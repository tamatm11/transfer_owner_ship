# Thêm tài khoản bằng Google OAuth với encrypted KV

Tool không còn dùng GitHub Gist để lưu account. Web app ghi token Google OAuth vào
Upstash/Vercel KV dưới dạng **mã hóa AES-256-GCM**; GitHub Actions chỉ đọc KV bằng
read-only token rồi giải mã bằng `OWNER_TOOL_STORE_KEY`.

## Luồng hoạt động

```text
Web (Vercel) -> /api/oauth/start -> Google consent -> /api/oauth/callback
                                                        |
                                                        | ghi encrypted bundle
                                                        v
                                                Upstash/Vercel KV
                                                        ^
                                                        | đọc read-only + giải mã
                                                GitHub Actions
```

KV key mặc định:

```text
owner-video-tool:accounts
```

Có thể đổi bằng `OWNER_TOOL_KV_KEY`.

## Setup 1 lần

### 1. Google OAuth client loại "Web application"

- Google Cloud Console -> APIs & Services -> Credentials -> Create credentials -> OAuth client ID.
- Application type: Web application.
- Authorized redirect URIs: `https://<domain-vercel>/api/oauth/callback`.
- Nếu app OAuth còn ở chế độ Testing, thêm email A/B vào Test users.
- Lưu lại Client ID và Client secret.

### 2. Tạo KV/Upstash trên Vercel

- Vercel project -> Storage/Marketplace -> Upstash Redis.
- Lấy các biến:
  - `KV_REST_API_URL`
  - `KV_REST_API_TOKEN`
  - `KV_REST_API_READ_ONLY_TOKEN`

Nếu dashboard dùng tên Upstash mới thì dùng cặp tương đương:

```text
UPSTASH_REDIS_REST_URL
UPSTASH_REDIS_REST_TOKEN
UPSTASH_REDIS_REST_READONLY_TOKEN
```

Code hiện tại hỗ trợ cả 2 nhóm tên.

### 3. Tạo store key mã hóa

Chạy local:

```powershell
node -e "console.log(require('crypto').randomBytes(32).toString('base64url'))"
```

Copy output làm `OWNER_TOOL_STORE_KEY`. Giữ key này thật kỹ: mất key là không giải mã
được account bundle trong KV.

### 4. Biến môi trường trên Vercel

Project Settings -> Environment Variables, thêm Production + Preview rồi redeploy:

```text
GOOGLE_WEB_CLIENT_ID
GOOGLE_WEB_CLIENT_SECRET
OWNER_TOOL_AUTH_SECRET
OWNER_TOOL_ALLOWED_EMAIL
OWNER_TOOL_PASSWORD_HASH
OWNER_TOOL_STORE_KEY
KV_REST_API_URL
KV_REST_API_TOKEN
KV_REST_API_READ_ONLY_TOKEN
```

Web cần `KV_REST_API_TOKEN` full quyền để thêm/xóa account.

### 5. Secret trên GitHub Actions

Repo -> Settings -> Secrets and variables -> Actions -> New repository secret:

```text
KV_REST_API_URL
KV_REST_API_READ_ONLY_TOKEN
OWNER_TOOL_STORE_KEY
CREDENTIALS_JSON_B64          # tùy chọn
```

GitHub Actions chỉ cần read-only token vì runner không ghi account.

## Migrate account cũ sang KV

Nếu `.env.local` đang có `OWNER_TOOL_ACCOUNTS_JSON_B64`, hoặc máy local còn
`account_a_accounts.json` / `account_b_accounts.json`, chạy:

```powershell
npm run migrate:kv
```

Script sẽ:

- đọc bundle account cũ,
- mã hóa bằng `OWNER_TOOL_STORE_KEY`,
- ghi vào KV key `owner-video-tool:accounts`.

Sau khi kiểm tra web đã thấy account A/B và GitHub Actions chạy được, có thể xóa:

```text
OWNER_TOOL_GIST_ID
GH_API_TOKEN dùng riêng cho gist
```

Nếu `GH_API_TOKEN` trước đây cũng dùng để dispatch workflow, hãy thay bằng
`GITHUB_DISPATCH_TOKEN` fine-grained chỉ có quyền Actions read/write cho repo này.

## Dùng hằng ngày

1. Mở web app -> Quản lý tài khoản.
2. Bấm Kết nối ở Account A hoặc B.
3. Login Google và đồng ý quyền Drive.
4. Token mới được merge vào encrypted KV store, không cần sửa GitHub secret hay redeploy.

## Lưu ý bảo mật

- Không dán KV token, `OWNER_TOOL_STORE_KEY`, refresh token Google vào chat/log/ticket.
- Nếu token KV từng bị dán công khai, rotate token trong Upstash/Vercel ngay.
- Nếu Gist cũ từng bị lộ URL, nên revoke Google OAuth permission rồi kết nối lại account.
