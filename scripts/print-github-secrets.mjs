// In ra 2 giá trị cần dán vào GitHub repo → Settings → Secrets and variables → Actions.
// Đọc OWNER_TOOL_ACCOUNTS_JSON_B64 từ .env.local (đã tạo bởi export-vercel-env)
// và base64 của credentials.json (nếu có). KHÔNG ghi ra file, chỉ in ra màn hình.
import fs from 'node:fs'
import path from 'node:path'
import process from 'node:process'

const ROOT = process.cwd()

function readEnvLocal() {
  const file = path.join(ROOT, '.env.local')
  if (!fs.existsSync(file)) {
    console.error('Chưa có .env.local. Chạy: npm run export:vercel-env -- --password "..." trước.')
    process.exit(1)
  }
  const out = {}
  for (const line of fs.readFileSync(file, 'utf8').split(/\r?\n/)) {
    const trimmed = line.trim()
    if (!trimmed || trimmed.startsWith('#')) continue
    const i = trimmed.indexOf('=')
    if (i <= 0) continue
    out[trimmed.slice(0, i).trim()] = trimmed.slice(i + 1).trim()
  }
  return out
}

const env = readEnvLocal()
const accounts = env.OWNER_TOOL_ACCOUNTS_JSON_B64
if (!accounts) {
  console.error('Không tìm thấy OWNER_TOOL_ACCOUNTS_JSON_B64 trong .env.local.')
  process.exit(1)
}

console.log('=== GitHub Secret #1 ===')
console.log('Name : OWNER_TOOL_ACCOUNTS_JSON_B64')
console.log('Value:')
console.log(accounts)
console.log('')

const credFile = path.join(ROOT, 'credentials.json')
if (fs.existsSync(credFile)) {
  const b64 = Buffer.from(fs.readFileSync(credFile)).toString('base64')
  console.log('=== GitHub Secret #2 (tùy chọn) ===')
  console.log('Name : CREDENTIALS_JSON_B64')
  console.log('Value:')
  console.log(b64)
} else {
  console.log('(Không thấy credentials.json — bỏ qua secret #2. Token account B vẫn refresh được nếu token chứa client_id/client_secret.)')
}
