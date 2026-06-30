// Shared encrypted account store backed by Upstash/Vercel KV REST.
//
// The KV value is an encrypted envelope. The plaintext bundle has this shape:
//
//   { "version": 1, "active_a": "owner@gmail.com",
//     "A": [{ "email", "display_name", "token_b64" }],
//     "B": [{ "email", "display_name", "token_b64" }] }
//
// Keep OWNER_TOOL_STORE_KEY in Vercel + GitHub Actions secrets. The KV token
// controls access to the value; the store key protects the Google refresh
// tokens even if the KV data is accidentally exposed.

import crypto from 'node:crypto'

const KV_KEY = 'owner-video-tool:accounts'

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

function kvUrl() {
  return (process.env.KV_REST_API_URL || process.env.UPSTASH_REDIS_REST_URL || '').trim().replace(/\/+$/, '')
}

function writeToken() {
  return (process.env.KV_REST_API_TOKEN || process.env.UPSTASH_REDIS_REST_TOKEN || '').trim()
}

function readToken() {
  return (
    writeToken()
    || (process.env.KV_REST_API_READ_ONLY_TOKEN || process.env.UPSTASH_REDIS_REST_READONLY_TOKEN || '').trim()
  )
}

function storeKey() {
  return (process.env.OWNER_TOOL_STORE_KEY || process.env.OWNER_TOOL_KV_ENCRYPTION_KEY || '').trim()
}

function accountKey() {
  return (process.env.OWNER_TOOL_KV_KEY || process.env.OWNER_TOOL_ACCOUNTS_KEY || KV_KEY).trim()
}

export function storeConfigured() {
  return Boolean(kvUrl() && writeToken() && storeKey())
}

function assertStoreConfigured() {
  if (!kvUrl() || !writeToken()) {
    throw Object.assign(new Error('Chưa cấu hình KV_REST_API_URL + KV_REST_API_TOKEN trên Vercel.'), { status: 500 })
  }
  if (!storeKey()) {
    throw Object.assign(new Error('Chưa cấu hình OWNER_TOOL_STORE_KEY để mã hóa token account.'), { status: 500 })
  }
}

function encryptionKey() {
  return crypto.createHash('sha256').update(storeKey(), 'utf8').digest()
}

function toBase64Url(buffer) {
  return Buffer.from(buffer).toString('base64url')
}

function fromBase64Url(value) {
  return Buffer.from(String(value || ''), 'base64url')
}

function encryptBundle(bundle) {
  const iv = crypto.randomBytes(12)
  const cipher = crypto.createCipheriv('aes-256-gcm', encryptionKey(), iv)
  const plaintext = Buffer.from(JSON.stringify(normalizeBundle(bundle)), 'utf8')
  const encrypted = Buffer.concat([cipher.update(plaintext), cipher.final()])
  return JSON.stringify({
    version: 2,
    encrypted: true,
    algorithm: 'aes-256-gcm',
    iv: toBase64Url(iv),
    tag: toBase64Url(cipher.getAuthTag()),
    data: toBase64Url(encrypted),
  })
}

function decryptBundle(raw) {
  if (!raw) return emptyBundle()
  const envelope = typeof raw === 'string' ? JSON.parse(raw) : raw
  if (!envelope?.encrypted) {
    throw Object.assign(new Error('KV account store chưa được mã hóa. Hãy chạy migration lại với OWNER_TOOL_STORE_KEY.'), { status: 500 })
  }
  if (envelope.algorithm !== 'aes-256-gcm' || envelope.version !== 2) {
    throw Object.assign(new Error('Định dạng KV account store không được hỗ trợ.'), { status: 500 })
  }
  const decipher = crypto.createDecipheriv('aes-256-gcm', encryptionKey(), fromBase64Url(envelope.iv))
  decipher.setAuthTag(fromBase64Url(envelope.tag))
  const decrypted = Buffer.concat([decipher.update(fromBase64Url(envelope.data)), decipher.final()])
  return normalizeBundle(JSON.parse(decrypted.toString('utf8')))
}

async function kvCommand(command, args, token) {
  const res = await fetch(kvUrl(), {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify([command, ...args]),
  })
  const text = await res.text()
  let data = null
  try {
    data = text ? JSON.parse(text) : null
  } catch {
    data = { error: text }
  }
  if (!res.ok || data?.error) {
    const detail = data?.error || text || 'unknown'
    throw Object.assign(new Error(`KV ${command} lỗi (${res.status}): ${detail}`), { status: 502 })
  }
  return data?.result
}

// Read the encrypted bundle from KV. Returns null when KV isn't configured so
// local/dev callers can still fall back to static env/local files.
export async function readBundle() {
  if (!kvUrl() || !readToken() || !storeKey()) return null
  const value = await kvCommand('GET', [accountKey()], readToken())
  return decryptBundle(value)
}

// Persist the encrypted bundle back to KV.
export async function writeBundle(bundle) {
  assertStoreConfigured()
  await kvCommand('SET', [accountKey(), encryptBundle(bundle)], writeToken())
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
