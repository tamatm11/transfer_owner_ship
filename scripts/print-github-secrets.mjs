// In ra các giá trị cần dán vào GitHub repo -> Settings -> Secrets and variables -> Actions.
// Không ghi ra file, chỉ in ra màn hình.
import fs from 'node:fs'
import path from 'node:path'
import process from 'node:process'

const ROOT = process.cwd()

function readEnvLocal() {
  const file = path.join(ROOT, '.env.local')
  const out = {}
  if (!fs.existsSync(file)) return out
  for (const line of fs.readFileSync(file, 'utf8').split(/\r?\n/)) {
    const trimmed = line.trim()
    if (!trimmed || trimmed.startsWith('#')) continue
    const i = trimmed.indexOf('=')
    if (i <= 0) continue
    out[trimmed.slice(0, i).trim()] = trimmed.slice(i + 1).trim().replace(/^"(.*)"$/, '$1')
  }
  return out
}

const env = { ...readEnvLocal(), ...process.env }

function printSecret(name, value, required = true) {
  if (!value) {
    if (required) console.error(`Thiếu ${name}.`)
    return
  }
  console.log(`=== GitHub Secret: ${name} ===`)
  console.log('Value:')
  console.log(value)
  console.log('')
}

printSecret('KV_REST_API_URL', env.KV_REST_API_URL || env.UPSTASH_REDIS_REST_URL)
printSecret('KV_REST_API_READ_ONLY_TOKEN', env.KV_REST_API_READ_ONLY_TOKEN || env.UPSTASH_REDIS_REST_READONLY_TOKEN)
printSecret('OWNER_TOOL_STORE_KEY', env.OWNER_TOOL_STORE_KEY)

const credFile = path.join(ROOT, 'credentials.json')
if (fs.existsSync(credFile)) {
  printSecret('CREDENTIALS_JSON_B64', Buffer.from(fs.readFileSync(credFile)).toString('base64'), false)
} else {
  console.log('(Không thấy credentials.json - bỏ qua CREDENTIALS_JSON_B64. Token account B vẫn refresh được nếu token chứa client_id/client_secret.)')
}
