import crypto from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'
import process from 'node:process'

const ROOT = process.cwd()
const KV_KEY = 'owner-video-tool:accounts'

function loadDotEnvLocal() {
  const file = path.join(ROOT, '.env.local')
  if (!fs.existsSync(file)) return
  for (const line of fs.readFileSync(file, 'utf8').split(/\r?\n/)) {
    const trimmed = line.trim()
    if (!trimmed || trimmed.startsWith('#')) continue
    const i = trimmed.indexOf('=')
    if (i <= 0) continue
    const key = trimmed.slice(0, i).trim()
    const value = trimmed.slice(i + 1).trim().replace(/^"(.*)"$/, '$1')
    if (!process.env[key]) process.env[key] = value
  }
}

function readJson(file) {
  return JSON.parse(fs.readFileSync(path.join(ROOT, file), 'utf8'))
}

function loadRegistry(file) {
  const registry = readJson(file)
  return {
    active_email: registry.active_email || '',
    accounts: (registry.accounts || []).map(account => {
      if (!fs.existsSync(account.token_path)) {
        throw new Error(`Không tìm thấy token: ${account.token_path}`)
      }
      return {
        email: account.email,
        display_name: account.display_name || '',
        token_b64: Buffer.from(fs.readFileSync(account.token_path, 'utf8'), 'utf8').toString('base64'),
      }
    }),
  }
}

function loadBundle() {
  const raw = (process.env.OWNER_TOOL_ACCOUNTS_JSON_B64 || '').trim()
  if (raw) return JSON.parse(Buffer.from(raw, 'base64').toString('utf8'))

  const accountA = loadRegistry('account_a_accounts.json')
  const accountB = loadRegistry('account_b_accounts.json')
  return {
    version: 1,
    active_a: accountA.active_email,
    A: accountA.accounts,
    B: accountB.accounts,
  }
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

function requiredEnv(name) {
  const value = (process.env[name] || '').trim()
  if (!value) throw new Error(`Thiếu ${name}`)
  return value
}

function storeKey() {
  return requiredEnv('OWNER_TOOL_STORE_KEY')
}

function kvUrl() {
  return (process.env.KV_REST_API_URL || process.env.UPSTASH_REDIS_REST_URL || '').trim().replace(/\/+$/, '')
}

function kvToken() {
  return (process.env.KV_REST_API_TOKEN || process.env.UPSTASH_REDIS_REST_TOKEN || '').trim()
}

function accountKey() {
  return (process.env.OWNER_TOOL_KV_KEY || process.env.OWNER_TOOL_ACCOUNTS_KEY || KV_KEY).trim()
}

function encryptBundle(bundle) {
  const iv = crypto.randomBytes(12)
  const key = crypto.createHash('sha256').update(storeKey(), 'utf8').digest()
  const cipher = crypto.createCipheriv('aes-256-gcm', key, iv)
  const encrypted = Buffer.concat([cipher.update(Buffer.from(JSON.stringify(normalizeBundle(bundle)), 'utf8')), cipher.final()])
  return JSON.stringify({
    version: 2,
    encrypted: true,
    algorithm: 'aes-256-gcm',
    iv: iv.toString('base64url'),
    tag: cipher.getAuthTag().toString('base64url'),
    data: encrypted.toString('base64url'),
  })
}

async function kvCommand(command, ...args) {
  if (!kvUrl()) throw new Error('Thiếu KV_REST_API_URL')
  if (!kvToken()) throw new Error('Thiếu KV_REST_API_TOKEN')
  const res = await fetch(kvUrl(), {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${kvToken()}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify([command, ...args]),
  })
  const text = await res.text()
  const data = text ? JSON.parse(text) : {}
  if (!res.ok || data.error) throw new Error(`KV ${command} lỗi (${res.status}): ${data.error || text}`)
  return data.result
}

loadDotEnvLocal()

if (!process.env.OWNER_TOOL_STORE_KEY) {
  console.error('Thiếu OWNER_TOOL_STORE_KEY. Tạo bằng:')
  console.error('node -e "console.log(require(\'crypto\').randomBytes(32).toString(\'base64url\'))"')
  process.exit(1)
}

const bundle = normalizeBundle(loadBundle())
if (!bundle.A.length && !bundle.B.length) {
  console.error('Không tìm thấy account nào để migrate.')
  process.exit(1)
}

await kvCommand('SET', accountKey(), encryptBundle(bundle))
console.log(`Đã migrate ${bundle.A.length} account A và ${bundle.B.length} account B sang encrypted KV key "${accountKey()}".`)
