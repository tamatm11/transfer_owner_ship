// Shared token store backed by a private GitHub Gist.
//
// The gist holds a single file `accounts.json` with the bundle that both this
// Vercel app and the GitHub Actions runner (gha_run.py) read at runtime:
//
//   { "version": 1, "active_a": "owner@gmail.com",
//     "A": [{ "email", "display_name", "token_b64" }],
//     "B": [{ "email", "display_name", "token_b64" }] }
//
// `token_b64` is base64 of the Google OAuth token JSON (same shape the Python
// CLI's load_credentials expects). Storing the bundle in a gist — instead of a
// static env var / GitHub secret — lets the web OAuth flow ADD accounts at
// runtime without a redeploy or a manual secret update. GitHub secrets are
// write-only (no read-back), so they cannot serve as the merge source; a gist
// can be read and rewritten with the same PAT.

const GIST_FILENAME = 'accounts.json'
const GH_API = 'https://api.github.com'

function token() {
  return (process.env.GH_API_TOKEN || process.env.GITHUB_DISPATCH_TOKEN || '').trim()
}

// Fine-grained PATs (prefix `github_pat_`) cannot write gists — GitHub rejects
// the PATCH with 409 "Gist cannot be updated". Only classic PATs (prefix `ghp_`)
// carrying the `gist` scope work. Detect the wrong kind up front so we fail with
// a clear message instead of an opaque 409 after the round-trip.
function tokenIsFineGrained() {
  return token().startsWith('github_pat_')
}

function gistId() {
  return (process.env.OWNER_TOOL_GIST_ID || '').trim()
}

export function storeConfigured() {
  return Boolean(token() && gistId())
}

function ghHeaders() {
  return {
    Authorization: `Bearer ${token()}`,
    Accept: 'application/vnd.github+json',
    'X-GitHub-Api-Version': '2022-11-28',
    'User-Agent': 'owner-video-tool',
  }
}

function emptyBundle() {
  return { version: 1, active_a: '', A: [], B: [] }
}

function normalizeBundle(raw) {
  const bundle = raw && typeof raw === 'object' ? raw : {}
  return {
    version: 1,
    active_a: String(bundle.active_a || bundle.active_email || '').trim(),
    A: Array.isArray(bundle.A) ? bundle.A : [],
    B: Array.isArray(bundle.B) ? bundle.B : [],
  }
}

// Read the bundle from the gist. Returns null when the store isn't configured
// (callers then fall back to the static env bundle).
export async function readBundle() {
  if (!storeConfigured()) return null
  const res = await fetch(`${GH_API}/gists/${gistId()}`, { headers: ghHeaders() })
  if (res.status === 404) return emptyBundle()
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw Object.assign(new Error(`Đọc gist token lỗi (${res.status}): ${text || 'unknown'}`), { status: 502 })
  }
  const data = await res.json()
  const file = data.files && data.files[GIST_FILENAME]
  if (!file) return emptyBundle()
  let content = file.content || ''
  // Gist truncates files >1MB; fetch the raw URL in that (unlikely) case.
  if (file.truncated && file.raw_url) {
    content = await fetch(file.raw_url, { headers: ghHeaders() }).then(r => r.text())
  }
  if (!content.trim()) return emptyBundle()
  try {
    return normalizeBundle(JSON.parse(content))
  } catch {
    throw Object.assign(new Error('Nội dung gist token không phải JSON hợp lệ'), { status: 502 })
  }
}

// Persist the bundle back to the gist (full replace of accounts.json).
export async function writeBundle(bundle) {
  if (!storeConfigured()) {
    throw Object.assign(new Error('Chưa cấu hình GH_API_TOKEN + OWNER_TOOL_GIST_ID'), { status: 500 })
  }
  if (tokenIsFineGrained()) {
    throw Object.assign(new Error(
      'GH_API_TOKEN đang là fine-grained token (github_pat_…) — GitHub không cho '
      + 'loại token này ghi gist nên sẽ luôn lỗi 409. Hãy tạo classic PAT '
      + '(https://github.com/settings/tokens) với scope `gist`, đặt lại GH_API_TOKEN '
      + 'trên Vercel rồi redeploy.'
    ), { status: 502 })
  }
  const body = JSON.stringify({
    files: { [GIST_FILENAME]: { content: JSON.stringify(normalizeBundle(bundle), null, 2) + '\n' } },
  })
  const res = await fetch(`${GH_API}/gists/${gistId()}`, { method: 'PATCH', headers: { ...ghHeaders(), 'Content-Type': 'application/json' }, body })
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    // 409 "Gist cannot be updated": GitHub từ chối ghi gist. Các nguyên nhân,
    // theo thứ tự phổ biến: (1) GH_API_TOKEN là fine-grained token (đã chặn ở
    // trên), (2) classic PAT nhưng THIẾU scope `gist`, (3) token không thuộc tài
    // khoản sở hữu gist OWNER_TOOL_GIST_ID.
    if (res.status === 409) {
      throw Object.assign(new Error(
        'Ghi gist token lỗi (409): GitHub từ chối cập nhật gist. Kiểm tra: '
        + '1) GH_API_TOKEN phải là classic PAT (ghp_…) có scope `gist`; '
        + '2) token phải thuộc chính tài khoản sở hữu gist OWNER_TOOL_GIST_ID. '
        + 'Tạo lại token tại https://github.com/settings/tokens rồi đặt lại trên Vercel và redeploy.'
      ), { status: 502 })
    }
    throw Object.assign(new Error(`Ghi gist token lỗi (${res.status}): ${text || 'unknown'}`), { status: 502 })
  }
}

// Insert or replace an account (by role + email, case-insensitive). Returns the
// updated bundle. The first account A added becomes active by default.
export function upsertAccount(bundle, role, account) {
  const next = normalizeBundle(bundle)
  const key = role === 'A' ? 'A' : 'B'
  const email = String(account.email || '').trim()
  next[key] = next[key].filter(item => String(item.email || '').toLowerCase() !== email.toLowerCase())
  next[key].push({ email, display_name: account.display_name || '', token_b64: account.token_b64 })
  next[key].sort((a, b) => String(a.email).toLowerCase().localeCompare(String(b.email).toLowerCase()))
  if (key === 'A' && !next.active_a) next.active_a = email
  return next
}

// Remove an account by role + email (case-insensitive). Returns the updated
// bundle. When the removed account was the active A, the first remaining A
// becomes active (or empty when none remain), so the bundle never points at a
// deleted owner.
export function removeAccount(bundle, role, email) {
  const next = normalizeBundle(bundle)
  const key = role === 'A' ? 'A' : 'B'
  const target = String(email || '').trim().toLowerCase()
  next[key] = next[key].filter(item => String(item.email || '').toLowerCase() !== target)
  if (key === 'A' && next.active_a.toLowerCase() === target) {
    next.active_a = next.A.length ? String(next.A[0].email || '') : ''
  }
  return next
}
