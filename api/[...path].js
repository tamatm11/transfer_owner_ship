import crypto from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { google } from 'googleapis'

export const config = { api: { bodyParser: true } }

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const ROOT = path.resolve(__dirname, '..')
const FOLDER_MIME = 'application/vnd.google-apps.folder'
const SHORTCUT_MIME = 'application/vnd.google-apps.shortcut'
const VIDEO_PREFIX = 'video/'
const ALLOWED_EMAIL = (process.env.OWNER_TOOL_ALLOWED_EMAIL || 'tamatm6713@gmail.com').trim().toLowerCase()
const SESSION_COOKIE = 'owner_tool_session'
const SESSION_TTL_SECONDS = 60 * 60 * 12

// GitHub Actions dispatch mode. When GITHUB_REPO + GITHUB_DISPATCH_TOKEN are set,
// jobs run server-side on a GitHub runner (fire-and-forget, up to 6h) instead of
// inside this serverless request. Without these, the in-request fallback runs.
const GITHUB_REPO = (process.env.GITHUB_REPO || '').trim()
const GITHUB_TOKEN = (process.env.GITHUB_DISPATCH_TOKEN || '').trim()
const GITHUB_WORKFLOW = (process.env.GITHUB_WORKFLOW_FILE || 'owner-tool.yml').trim()
const GITHUB_REF = (process.env.GITHUB_REF || 'main').trim()
const DISPATCH_MODE = Boolean(GITHUB_REPO && GITHUB_TOKEN)

const jobs = globalThis.__ownerToolJobs || new Map()
globalThis.__ownerToolJobs = jobs
globalThis.__ownerToolActiveA = globalThis.__ownerToolActiveA || ''

function json(res, status, payload) {
  res.status(status).setHeader('Content-Type', 'application/json; charset=utf-8')
  res.end(JSON.stringify(payload))
}

function methodNotAllowed(res) {
  json(res, 405, { message: 'Method not allowed' })
}

function readCookie(req, name) {
  const raw = req.headers.cookie || ''
  for (const part of raw.split(';')) {
    const [key, ...rest] = part.trim().split('=')
    if (key === name) return decodeURIComponent(rest.join('=') || '')
  }
  return ''
}

function serializeCookie(name, value, options = {}) {
  const parts = [`${name}=${encodeURIComponent(value)}`]
  parts.push('Path=/')
  parts.push('HttpOnly')
  parts.push('SameSite=Lax')
  if (process.env.NODE_ENV === 'production') parts.push('Secure')
  if (options.maxAge !== undefined) parts.push(`Max-Age=${options.maxAge}`)
  return parts.join('; ')
}

function base64url(input) {
  return Buffer.from(input).toString('base64url')
}

function sign(value) {
  const secret = process.env.OWNER_TOOL_AUTH_SECRET
  if (!secret) throw new Error('OWNER_TOOL_AUTH_SECRET is missing')
  return crypto.createHmac('sha256', secret).update(value).digest('base64url')
}

function createSession(email) {
  const payload = base64url(JSON.stringify({ email, exp: Math.floor(Date.now() / 1000) + SESSION_TTL_SECONDS }))
  return `${payload}.${sign(payload)}`
}

function verifySession(req) {
  try {
    const token = readCookie(req, SESSION_COOKIE)
    if (!token || !token.includes('.')) return null
    const [payload, signature] = token.split('.', 2)
    const expected = sign(payload)
    const actualBuffer = Buffer.from(signature)
    const expectedBuffer = Buffer.from(expected)
    if (actualBuffer.length !== expectedBuffer.length) return null
    if (!crypto.timingSafeEqual(actualBuffer, expectedBuffer)) return null
    const data = JSON.parse(Buffer.from(payload, 'base64url').toString('utf8'))
    if (!data?.email || data.exp < Math.floor(Date.now() / 1000)) return null
    if (String(data.email).toLowerCase() !== ALLOWED_EMAIL) return null
    return { email: data.email }
  } catch {
    return null
  }
}

function timingSafeTextEqual(left, right) {
  const a = Buffer.from(String(left))
  const b = Buffer.from(String(right))
  if (a.length !== b.length) return false
  return crypto.timingSafeEqual(a, b)
}

function verifyPassword(password) {
  const plain = process.env.OWNER_TOOL_PASSWORD
  const hash = process.env.OWNER_TOOL_PASSWORD_HASH
  if (plain) return timingSafeTextEqual(password, plain)
  if (hash) {
    const [algorithm, salt, key] = hash.split(':')
    if (algorithm !== 'scrypt' || !salt || !key) return false
    const derived = crypto.scryptSync(String(password), salt, key.length / 2).toString('hex')
    return timingSafeTextEqual(derived, key)
  }
  throw new Error('OWNER_TOOL_PASSWORD or OWNER_TOOL_PASSWORD_HASH is missing')
}

function readJsonEnv(...names) {
  for (const name of names) {
    const value = process.env[name]
    if (!value) continue
    const raw = value.trim()
    try {
      return JSON.parse(raw)
    } catch {
      try {
        return JSON.parse(Buffer.from(raw, 'base64').toString('utf8'))
      } catch {
        throw new Error(`${name} is not valid JSON/base64 JSON`)
      }
    }
  }
  return null
}

function normalizeToken(raw) {
  if (!raw) return null
  if (typeof raw === 'string') {
    try {
      return JSON.parse(raw)
    } catch {
      return JSON.parse(Buffer.from(raw, 'base64').toString('utf8'))
    }
  }
  if (raw.token_b64) return JSON.parse(Buffer.from(raw.token_b64, 'base64').toString('utf8'))
  if (raw.token_json) return typeof raw.token_json === 'string' ? JSON.parse(raw.token_json) : raw.token_json
  if (raw.token) return raw.token
  return raw
}

function loadRegistry(registryFile) {
  const full = path.join(ROOT, registryFile)
  if (!fs.existsSync(full)) return { accounts: [], active_email: '' }
  const data = JSON.parse(fs.readFileSync(full, 'utf8'))
  const accounts = []
  for (const item of data.accounts || []) {
    if (!item.email || !item.token_path || !fs.existsSync(item.token_path)) continue
    accounts.push({
      email: item.email,
      display_name: item.display_name || '',
      token: JSON.parse(fs.readFileSync(item.token_path, 'utf8')),
    })
  }
  return { accounts, active_email: data.active_email || '' }
}

function loadAccounts() {
  const bundled = readJsonEnv('OWNER_TOOL_ACCOUNTS_JSON', 'OWNER_TOOL_ACCOUNTS_JSON_B64')
  let activeA = ''
  let accountsA = []
  let accountsB = []

  if (bundled) {
    activeA = bundled.active_a || bundled.active_email || ''
    accountsA = bundled.A || bundled.accountsA || bundled.account_a || []
    accountsB = bundled.B || bundled.accountsB || bundled.account_b || []
  } else {
    const envA = readJsonEnv('OWNER_TOOL_ACCOUNT_A_JSON', 'OWNER_TOOL_ACCOUNT_A_JSON_B64')
    const envB = readJsonEnv('OWNER_TOOL_ACCOUNT_B_JSON', 'OWNER_TOOL_ACCOUNT_B_JSON_B64')
    if (envA || envB) {
      accountsA = Array.isArray(envA) ? envA : (envA?.accounts || envA?.A || [])
      accountsB = Array.isArray(envB) ? envB : (envB?.accounts || envB?.B || [])
      activeA = envA?.active_email || envA?.active_a || ''
    } else {
      const localA = loadRegistry('account_a_accounts.json')
      const localB = loadRegistry('account_b_accounts.json')
      accountsA = localA.accounts
      accountsB = localB.accounts
      activeA = localA.active_email
    }
  }

  const normalize = (items, role) => (items || [])
    .map(item => ({
      role,
      email: String(item.email || '').trim(),
      display_name: item.display_name || item.displayName || '',
      token: normalizeToken(item),
    }))
    .filter(item => item.email && item.token)

  const A = normalize(accountsA, 'A')
  const B = normalize(accountsB, 'B')
  if (!activeA && A.length) activeA = A[0].email
  if (globalThis.__ownerToolActiveA) activeA = globalThis.__ownerToolActiveA
  return { A, B, activeA }
}

function publicAccount(account, active = false) {
  return { role: account.role, email: account.email, display_name: account.display_name || '', active }
}

function findAccount(role, email) {
  const accounts = loadAccounts()
  return (role === 'A' ? accounts.A : accounts.B).find(item => item.email.toLowerCase() === String(email).toLowerCase())
}

function driveFromToken(token) {
  const clientId = token.client_id || token.clientId
  const clientSecret = token.client_secret || token.clientSecret
  const redirectUri = 'urn:ietf:wg:oauth:2.0:oob'
  const auth = new google.auth.OAuth2(clientId, clientSecret, redirectUri)
  auth.setCredentials({
    access_token: token.access_token || token.token,
    refresh_token: token.refresh_token,
    token_type: token.token_type || 'Bearer',
    scope: Array.isArray(token.scopes) ? token.scopes.join(' ') : token.scope,
    expiry_date: token.expiry_date || (token.expiry ? Date.parse(token.expiry) : undefined),
  })
  return google.drive({ version: 'v3', auth })
}

function extractFolderId(value) {
  const text = String(value || '').trim()
  if (!text) throw new Error('Folder URL/ID cannot be blank')
  const patterns = [
    /\/folders\/([a-zA-Z0-9_-]+)/,
    /[?&]id=([a-zA-Z0-9_-]+)/,
    /\/d\/([a-zA-Z0-9_-]+)/,
  ]
  for (const pattern of patterns) {
    const match = text.match(pattern)
    if (match) return match[1]
  }
  if (/^[a-zA-Z0-9_-]{10,}$/.test(text)) return text
  throw new Error(`Không nhận diện được Drive ID: ${text}`)
}

async function getFile(drive, fileId) {
  const { data } = await drive.files.get({
    fileId,
    fields: 'id,name,mimeType,owners(emailAddress),copyRequiresWriterPermission',
    supportsAllDrives: true,
  })
  return { id: data.id, name: data.name || data.id, mimeType: data.mimeType || '', owners: data.owners || [] }
}

async function listChildren(drive, folderId) {
  const items = []
  let pageToken
  do {
    const { data } = await drive.files.list({
      q: `'${folderId.replace(/'/g, "\\'")}' in parents and trashed = false`,
      fields: 'nextPageToken,files(id,name,mimeType)',
      pageSize: 1000,
      pageToken,
      supportsAllDrives: true,
      includeItemsFromAllDrives: true,
    })
    items.push(...(data.files || []))
    pageToken = data.nextPageToken
  } while (pageToken)
  return items.map(item => ({ id: item.id, name: item.name || item.id, mimeType: item.mimeType || '' }))
}

function isVideo(item) {
  return String(item.mimeType || '').startsWith(VIDEO_PREFIX)
}

async function collectVideos(drive, folderIds, recursive, logs) {
  const videos = []
  const seenVideos = new Set()
  const visitedFolders = new Set()
  for (const folderId of folderIds) {
    const root = await getFile(drive, folderId)
    if (root.mimeType !== FOLDER_MIME) {
      if (isVideo(root) && !seenVideos.has(root.id)) {
        seenVideos.add(root.id)
        videos.push(root)
      }
      continue
    }
    const queue = [root]
    while (queue.length) {
      const folder = queue.shift()
      if (visitedFolders.has(folder.id)) continue
      visitedFolders.add(folder.id)
      logs.push(`[scan] ${folder.name} — videos=${videos.length}`)
      for (const child of await listChildren(drive, folder.id)) {
        if (child.mimeType === FOLDER_MIME) {
          if (recursive) queue.push(child)
          continue
        }
        if (child.mimeType === SHORTCUT_MIME) continue
        if (isVideo(child) && !seenVideos.has(child.id)) {
          seenVideos.add(child.id)
          videos.push(child)
        }
      }
    }
  }
  return videos
}

async function collectTransferItems(drive, folderIds, recursive, scope, logs) {
  const includeVideos = scope === 'videos' || scope === 'all'
  const includeFolders = scope === 'folders' || scope === 'all'
  const videos = []
  const folders = []
  const seenVideos = new Set()
  const seenFolders = new Set()
  for (const folderId of folderIds) {
    const root = await getFile(drive, folderId)
    if (root.mimeType !== FOLDER_MIME) {
      if (includeVideos && isVideo(root) && !seenVideos.has(root.id)) {
        seenVideos.add(root.id)
        videos.push(root)
      }
      continue
    }
    const queue = [{ item: root, depth: 0 }]
    while (queue.length) {
      const { item: folder, depth } = queue.shift()
      if (seenFolders.has(folder.id)) continue
      seenFolders.add(folder.id)
      if (includeFolders) folders.push({ depth, item: folder })
      logs.push(`[scan] ${folder.name} — videos=${videos.length} folders=${folders.length}`)
      for (const child of await listChildren(drive, folder.id)) {
        if (child.mimeType === FOLDER_MIME) {
          if (recursive) queue.push({ item: child, depth: depth + 1 })
          continue
        }
        if (child.mimeType === SHORTCUT_MIME) continue
        if (includeVideos && isVideo(child) && !seenVideos.has(child.id)) {
          seenVideos.add(child.id)
          videos.push(child)
        }
      }
    }
  }
  const folderItems = folders.sort((a, b) => b.depth - a.depth).map(entry => entry.item)
  if (scope === 'folders') return folderItems
  if (scope === 'all') return [...videos, ...folderItems]
  return videos
}

async function findPermission(drive, fileId, email) {
  let pageToken
  const target = String(email).toLowerCase()
  do {
    const { data } = await drive.permissions.list({
      fileId,
      fields: 'nextPageToken,permissions(id,emailAddress,type,role,pendingOwner)',
      pageSize: 100,
      pageToken,
      supportsAllDrives: true,
    })
    const found = (data.permissions || []).find(p => String(p.emailAddress || '').toLowerCase() === target)
    if (found) return found
    pageToken = data.nextPageToken
  } while (pageToken)
  return null
}

async function transferWorkspace(drive, fileId, email, notify) {
  const existing = await findPermission(drive, fileId, email)
  if (existing?.role === 'owner' && !existing.pendingOwner) return existing.id
  if (existing) {
    await drive.permissions.update({
      fileId,
      permissionId: existing.id,
      requestBody: { role: 'owner' },
      transferOwnership: true,
      sendNotificationEmail: notify,
      fields: 'id,emailAddress,role',
      supportsAllDrives: true,
    })
    return existing.id
  }
  const { data } = await drive.permissions.create({
    fileId,
    requestBody: { type: 'user', role: 'owner', emailAddress: email },
    transferOwnership: true,
    sendNotificationEmail: notify,
    fields: 'id,emailAddress,role',
    supportsAllDrives: true,
  })
  return data.id
}

async function transferConsumer(ownerDrive, acceptDrive, item, email, notify) {
  const existing = await findPermission(ownerDrive, item.id, email)
  if (existing?.role === 'owner' && !existing.pendingOwner) return existing.id
  let permissionId
  if (existing) {
    permissionId = existing.id
    await ownerDrive.permissions.update({
      fileId: item.id,
      permissionId,
      requestBody: { role: 'writer', pendingOwner: true },
      fields: 'id,emailAddress,role,pendingOwner',
      supportsAllDrives: true,
    })
  } else {
    const { data } = await ownerDrive.permissions.create({
      fileId: item.id,
      requestBody: { type: 'user', role: 'writer', emailAddress: email, pendingOwner: true },
      sendNotificationEmail: true,
      fields: 'id,emailAddress,role,pendingOwner',
      supportsAllDrives: true,
    })
    permissionId = data.id
  }
  if (!acceptDrive) return permissionId
  try {
    await acceptDrive.permissions.update({
      fileId: item.id,
      permissionId,
      requestBody: { role: 'owner' },
      transferOwnership: true,
      fields: 'id,emailAddress,role',
      supportsAllDrives: true,
    })
  } catch (error) {
    if (error?.code !== 404) throw error
    await acceptDrive.permissions.create({
      fileId: item.id,
      requestBody: { type: 'user', role: 'owner', emailAddress: email },
      transferOwnership: true,
      sendNotificationEmail: notify,
      fields: 'id,emailAddress,role',
      supportsAllDrives: true,
    })
  }
  return permissionId
}

async function setCopyRestriction(drive, fileId, restricted) {
  await drive.files.update({
    fileId,
    requestBody: { copyRequiresWriterPermission: restricted },
    fields: 'id,copyRequiresWriterPermission',
    supportsAllDrives: true,
  })
}

function newJob(type, logs, status = 'completed', returnCode = 0) {
  const id = crypto.randomUUID()
  const job = {
    id,
    job_id: id,
    type,
    status,
    created_at: new Date().toISOString(),
    finished_at: new Date().toISOString(),
    return_code: returnCode,
    logs,
    next_log_offset: logs.length,
    progress: status === 'completed' ? 100 : 0,
  }
  jobs.set(id, job)
  return job
}

async function githubFetch(pathStr, init = {}) {
  return fetch(`https://api.github.com${pathStr}`, {
    ...init,
    headers: {
      Authorization: `Bearer ${GITHUB_TOKEN}`,
      Accept: 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
      'User-Agent': 'owner-video-tool',
      ...(init.headers || {}),
    },
  })
}

function buildTransferPayload(body) {
  const accounts = loadAccounts()
  const owner = accounts.A.find(item => item.email.toLowerCase() === String(body.owner_email || '').toLowerCase())
  if (!owner) throw Object.assign(new Error('Account A chưa đăng ký'), { status: 400 })
  const rows = Array.isArray(body.rows) ? body.rows : []
  if (!rows.length) throw Object.assign(new Error('Cần ít nhất một dòng transfer'), { status: 422 })
  const outRows = rows.map(row => {
    const receiver = accounts.B.find(item => item.email.toLowerCase() === String(row.receiver_email || '').toLowerCase())
    if (!receiver) throw Object.assign(new Error(`Account B chưa đăng ký: ${row.receiver_email}`), { status: 400 })
    const folders = (row.folders || []).map(extractFolderId)
    if (!folders.length) throw Object.assign(new Error(`Dòng nhận ${row.receiver_email} chưa có folder`), { status: 422 })
    return { receiver_email: receiver.email, folders }
  })
  return {
    owner_email: owner.email,
    mode: body.mode === 'workspace' ? 'workspace' : 'consumer',
    scope: ['videos', 'folders', 'all'].includes(body.scope) ? body.scope : 'videos',
    no_recursive: body.recursive === false,
    no_notify: Boolean(body.no_notify),
    dry_run: Boolean(body.dry_run),
    rows: outRows,
  }
}

function buildBlockPayload(body) {
  const owner = findAccount('A', body.owner_email)
  if (!owner) throw Object.assign(new Error('Account A chưa đăng ký'), { status: 400 })
  const folders = (body.folders || []).map(extractFolderId)
  if (!folders.length) throw Object.assign(new Error('Cần ít nhất một folder'), { status: 422 })
  return {
    owner_email: owner.email,
    folders,
    recursive: body.recursive !== false,
    unblock: Boolean(body.unblock),
    dry_run: Boolean(body.dry_run),
  }
}

async function dispatchJob(kind, payload) {
  const jobId = crypto.randomUUID()
  const inputs = { kind, job_id: jobId, payload: JSON.stringify(payload) }
  const res = await githubFetch(`/repos/${GITHUB_REPO}/actions/workflows/${encodeURIComponent(GITHUB_WORKFLOW)}/dispatches`, {
    method: 'POST',
    body: JSON.stringify({ ref: GITHUB_REF, inputs }),
  })
  if (res.status !== 204) {
    const text = await res.text().catch(() => '')
    throw Object.assign(new Error(`GitHub dispatch lỗi (${res.status}): ${text || 'unknown'}`), { status: 502 })
  }
  return {
    id: jobId,
    job_id: jobId,
    type: kind,
    status: 'queued',
    runner: 'github',
    created_at: new Date().toISOString(),
    progress: 5,
    logs: ['Đã gửi job sang GitHub Actions. Runner đang khởi động (~20–40s)…'],
  }
}

function mapRunStatus(run) {
  if (run.status !== 'completed') return run.status === 'in_progress' ? 'running' : 'queued'
  if (run.conclusion === 'success') return 'completed'
  if (run.conclusion === 'cancelled') return 'stopped'
  return 'failed'
}

async function findRun(jobId) {
  const res = await githubFetch(`/repos/${GITHUB_REPO}/actions/runs?event=workflow_dispatch&per_page=40`)
  if (!res.ok) return null
  const data = await res.json().catch(() => ({}))
  const runs = data.workflow_runs || []
  return runs.find(run => `${run.name || ''} ${run.display_title || ''}`.includes(jobId)) || null
}

async function githubJobStatus(jobId) {
  const run = await findRun(jobId)
  if (!run) {
    return { id: jobId, job_id: jobId, status: 'queued', runner: 'github', progress: 5, logs: ['Đang chờ GitHub Actions nhận job…'] }
  }
  const status = mapRunStatus(run)
  const kindMatch = String(run.name || '').match(/owner-tool\s+(\w+)/)
  const progress = status === 'completed' ? 100 : status === 'running' ? 50 : status === 'queued' ? 10 : 100
  return {
    id: jobId,
    job_id: jobId,
    type: kindMatch ? kindMatch[1] : undefined,
    status,
    runner: 'github',
    run_id: run.id,
    run_url: run.html_url,
    created_at: run.created_at,
    finished_at: run.status === 'completed' ? run.updated_at : undefined,
    progress,
    return_code: status === 'completed' ? 0 : status === 'failed' ? 1 : undefined,
    logs: [
      `GitHub Actions run #${run.run_number} · ${status}`,
      `Xem log trực tiếp tại: ${run.html_url}`,
    ],
  }
}

async function githubCancel(jobId) {
  const run = await findRun(jobId)
  if (!run) return { id: jobId, job_id: jobId, status: 'queued', runner: 'github' }
  if (run.status !== 'completed') {
    await githubFetch(`/repos/${GITHUB_REPO}/actions/runs/${run.id}/cancel`, { method: 'POST' }).catch(() => {})
  }
  return {
    id: jobId,
    job_id: jobId,
    status: 'stopped',
    runner: 'github',
    run_id: run.id,
    run_url: run.html_url,
    logs: ['Đã yêu cầu dừng job trên GitHub Actions.'],
  }
}

async function handleTransfer(body) {
  const logs = []
  const accounts = loadAccounts()
  const owner = accounts.A.find(item => item.email.toLowerCase() === String(body.owner_email || '').toLowerCase())
  if (!owner) throw Object.assign(new Error('Account A is not registered'), { status: 400 })
  const ownerDrive = driveFromToken(owner.token)
  const rows = Array.isArray(body.rows) ? body.rows : []
  if (!rows.length) throw Object.assign(new Error('Cần ít nhất một dòng transfer'), { status: 422 })
  let success = 0
  let failed = 0
  for (const row of rows) {
    const receiver = accounts.B.find(item => item.email.toLowerCase() === String(row.receiver_email || '').toLowerCase())
    if (!receiver) throw Object.assign(new Error(`Account B is not registered: ${row.receiver_email}`), { status: 400 })
    const folderIds = (row.folders || []).map(extractFolderId)
    const acceptDrive = body.mode === 'consumer' && !body.dry_run ? driveFromToken(receiver.token) : null
    const items = await collectTransferItems(ownerDrive, folderIds, body.recursive !== false, body.scope || 'videos', logs)
    const videoCount = items.filter(isVideo).length
    const folderCount = items.filter(item => item.mimeType === FOLDER_MIME).length
    logs.push(`Resolved ${folderIds.length} folder(s). Selected ${videoCount} video(s), ${folderCount} folder(s). mode=${body.mode || 'consumer'} dry_run=${Boolean(body.dry_run)}`)
    for (const item of items) {
      const label = `${item.name} (${item.id})`
      if (body.dry_run) {
        success += 1
        logs.push(`[DRY]  ${label}`)
        continue
      }
      try {
        if (body.mode === 'workspace') await transferWorkspace(ownerDrive, item.id, receiver.email, !body.no_notify)
        else await transferConsumer(ownerDrive, acceptDrive, item, receiver.email, !body.no_notify)
        success += 1
        logs.push(`[OK]   ${label}`)
      } catch (error) {
        failed += 1
        logs.push(`[ERR]  ${label}: ${error.message || error}`)
      }
    }
  }
  logs.push(`Done. transferred=${success}, failed=${failed}`)
  return newJob('transfer', logs, failed ? 'failed' : 'completed', failed ? 1 : 0)
}

async function handleBlock(body) {
  const logs = []
  const owner = findAccount('A', body.owner_email)
  if (!owner) throw Object.assign(new Error('Account A is not registered'), { status: 400 })
  const drive = driveFromToken(owner.token)
  const folderIds = (body.folders || []).map(extractFolderId)
  if (!folderIds.length) throw Object.assign(new Error('Cần ít nhất một folder'), { status: 422 })
  const videos = await collectVideos(drive, folderIds, body.recursive !== false, logs)
  const restricted = !body.unblock
  const action = restricted ? 'BLOCK' : 'UNBLOCK'
  logs.push(`Found ${videos.length} video(s) across ${folderIds.length} folder(s). action=${action} dry_run=${Boolean(body.dry_run)}`)
  let success = 0
  let failed = 0
  for (const item of videos) {
    const label = `${item.name} (${item.id})`
    if (body.dry_run) {
      success += 1
      logs.push(`[DRY]  ${action} ${label}`)
      continue
    }
    try {
      await setCopyRestriction(drive, item.id, restricted)
      success += 1
      logs.push(`[OK]   ${action} ${label}`)
    } catch (error) {
      failed += 1
      logs.push(`[ERR]  ${label}: ${error.message || error}`)
    }
  }
  logs.push(`Done. ${action.toLowerCase()}ed=${success}, failed=${failed}`)
  return newJob('block', logs, failed ? 'failed' : 'completed', failed ? 1 : 0)
}

async function getBody(req) {
  if (req.body && typeof req.body === 'object') return req.body
  if (typeof req.body === 'string' && req.body) return JSON.parse(req.body)
  return {}
}

export default async function handler(req, res) {
  try {
    const rawPath = req.query.path || []
    const parts = (Array.isArray(rawPath) ? rawPath : [rawPath]).filter(Boolean)
    const route = `/${parts.join('/')}`

    if (route === '/health') return json(res, 200, { ok: true, service: 'owner-video-tool-node' })

    if (route === '/auth/session' && req.method === 'GET') {
      const user = verifySession(req)
      return json(res, 200, { authenticated: Boolean(user), user })
    }

    if (route === '/auth/login' && req.method === 'POST') {
      const body = await getBody(req)
      const email = String(body.email || ALLOWED_EMAIL).trim().toLowerCase()
      if (email !== ALLOWED_EMAIL || !verifyPassword(String(body.password || ''))) {
        return json(res, 401, { message: 'Email hoặc mật khẩu không đúng' })
      }
      res.setHeader('Set-Cookie', serializeCookie(SESSION_COOKIE, createSession(email), { maxAge: SESSION_TTL_SECONDS }))
      return json(res, 200, { user: { email } })
    }

    if (route === '/auth/logout' && req.method === 'POST') {
      res.setHeader('Set-Cookie', serializeCookie(SESSION_COOKIE, '', { maxAge: 0 }))
      return json(res, 200, { ok: true })
    }

    const user = verifySession(req)
    if (!user) return json(res, 401, { message: 'Vui lòng đăng nhập lại' })

    if (route === '/accounts' && req.method === 'GET') {
      const accounts = loadAccounts()
      const publicA = accounts.A.map(item => publicAccount(item, item.email.toLowerCase() === accounts.activeA.toLowerCase()))
      const publicB = accounts.B.map(item => publicAccount(item))
      return json(res, 200, { A: publicA, B: publicB, active_a: accounts.activeA || null, accounts: [...publicA, ...publicB] })
    }

    if (route === '/accounts/oauth' && req.method === 'POST') {
      return json(res, 501, { message: 'Bản Vercel dùng token trong Environment Variables. Hãy chạy npm run export:vercel-env trên máy local rồi thêm các biến env lên Vercel.' })
    }

    if (parts[0] === 'oauth' && req.method === 'GET') {
      return json(res, 404, { message: 'OAuth local không khả dụng trên bản Vercel' })
    }

    if (parts[0] === 'accounts' && parts[3] === 'activate' && req.method === 'POST') {
      const role = String(parts[1] || '').toUpperCase()
      const email = decodeURIComponent(parts[2] || '')
      if (role !== 'A') return json(res, 400, { message: 'Only account A can be activated' })
      const account = findAccount('A', email)
      if (!account) return json(res, 404, { message: 'Account A not found' })
      globalThis.__ownerToolActiveA = account.email
      return json(res, 200, publicAccount(account, true))
    }

    if (route === '/jobs/transfer' && req.method === 'POST') {
      const body = await getBody(req)
      if (DISPATCH_MODE) return json(res, 202, await dispatchJob('transfer', buildTransferPayload(body)))
      return json(res, 202, await handleTransfer(body))
    }

    if (route === '/jobs/block' && req.method === 'POST') {
      const body = await getBody(req)
      if (DISPATCH_MODE) return json(res, 202, await dispatchJob('block', buildBlockPayload(body)))
      return json(res, 202, await handleBlock(body))
    }

    if (parts[0] === 'jobs' && parts[1] && parts[2] === 'stop' && req.method === 'POST') {
      if (DISPATCH_MODE) return json(res, 200, await githubCancel(parts[1]))
      const job = jobs.get(parts[1])
      if (!job) return json(res, 404, { message: 'Job not found' })
      return json(res, 200, { ...job, status: job.status === 'running' ? 'stopped' : job.status })
    }

    if (parts[0] === 'jobs' && parts[1] && req.method === 'GET') {
      if (DISPATCH_MODE) return json(res, 200, await githubJobStatus(parts[1]))
      const job = jobs.get(parts[1])
      if (!job) return json(res, 404, { message: 'Job not found' })
      return json(res, 200, job)
    }

    return methodNotAllowed(res)
  } catch (error) {
    const status = error.status || error.code || 500
    return json(res, Number.isInteger(status) && status >= 400 && status < 600 ? status : 500, {
      message: error.message || 'Server error',
    })
  }
}
