// Web (redirect-based) Google OAuth for the deployed app.
//
// The local tool (web_server.py) uses an installed-app loopback flow, which
// can't run on Vercel serverless. Here we use the standard web flow: redirect
// the user to Google, then handle the callback on /api/oauth/callback, exchange
// the code, and capture a refresh token. Requires a Google OAuth client of type
// "Web application" whose authorized redirect URI is this app's
// https://<host>/api/oauth/callback.

import crypto from 'node:crypto'
import { google } from 'googleapis'

const SCOPES = ['https://www.googleapis.com/auth/drive']
const TOKEN_URI = 'https://oauth2.googleapis.com/token'
const STATE_TTL_SECONDS = 15 * 60

function webCredentials() {
  // Either two discrete env vars, or the whole downloaded client_secret JSON.
  const id = (process.env.GOOGLE_WEB_CLIENT_ID || '').trim()
  const secret = (process.env.GOOGLE_WEB_CLIENT_SECRET || '').trim()
  if (id && secret) return { clientId: id, clientSecret: secret }
  const raw = (process.env.GOOGLE_WEB_CREDENTIALS_JSON || '').trim()
  if (raw) {
    let parsed
    try {
      parsed = JSON.parse(raw)
    } catch {
      parsed = JSON.parse(Buffer.from(raw, 'base64').toString('utf8'))
    }
    const web = parsed.web || parsed.installed || parsed
    if (web.client_id && web.client_secret) {
      return { clientId: web.client_id, clientSecret: web.client_secret }
    }
  }
  return null
}

export function oauthConfigured() {
  return Boolean(webCredentials())
}

export function callbackUrl(req) {
  const host = String(req.headers['x-forwarded-host'] || req.headers.host || '').trim()
  const proto = String(req.headers['x-forwarded-proto'] || 'https').split(',')[0].trim() || 'https'
  return `${proto}://${host}/api/oauth/callback`
}

function client(redirectUri) {
  const creds = webCredentials()
  if (!creds) throw Object.assign(new Error('Chưa cấu hình GOOGLE_WEB_CLIENT_ID/SECRET'), { status: 500 })
  return new google.auth.OAuth2(creds.clientId, creds.clientSecret, redirectUri)
}

function stateSecret() {
  const secret = process.env.OWNER_TOOL_AUTH_SECRET
  if (!secret) throw new Error('OWNER_TOOL_AUTH_SECRET is missing')
  return secret
}

// Signed, time-limited state to defend the callback against CSRF and to carry
// which role (A/B) the login is for.
export function signState(role) {
  const payload = Buffer.from(JSON.stringify({
    role,
    nonce: crypto.randomBytes(8).toString('hex'),
    exp: Math.floor(Date.now() / 1000) + STATE_TTL_SECONDS,
  })).toString('base64url')
  const sig = crypto.createHmac('sha256', stateSecret()).update(payload).digest('base64url')
  return `${payload}.${sig}`
}

export function verifyState(state) {
  if (!state || !state.includes('.')) return null
  const [payload, sig] = state.split('.', 2)
  const expected = crypto.createHmac('sha256', stateSecret()).update(payload).digest('base64url')
  const a = Buffer.from(sig)
  const b = Buffer.from(expected)
  if (a.length !== b.length || !crypto.timingSafeEqual(a, b)) return null
  try {
    const data = JSON.parse(Buffer.from(payload, 'base64url').toString('utf8'))
    if (!data || (data.role !== 'A' && data.role !== 'B')) return null
    if (data.exp < Math.floor(Date.now() / 1000)) return null
    return data
  } catch {
    return null
  }
}

export function buildAuthUrl(redirectUri, state) {
  return client(redirectUri).generateAuthUrl({
    access_type: 'offline',
    prompt: 'select_account consent', // force a refresh token even on re-consent
    scope: SCOPES,
    state,
    include_granted_scopes: true,
  })
}

// Exchange the authorization code, fetch the account email, and return both the
// account identity and a token JSON in the shape the Python CLI expects
// (google.oauth2.credentials.Credentials.from_authorized_user_info).
export async function exchangeCode(redirectUri, code) {
  const auth = client(redirectUri)
  const { tokens } = await auth.getToken(code)
  if (!tokens.refresh_token) {
    throw Object.assign(new Error('Google không trả refresh_token. Hãy gỡ quyền ứng dụng tại myaccount.google.com/permissions rồi thử lại.'), { status: 400 })
  }
  auth.setCredentials(tokens)
  const drive = google.drive({ version: 'v3', auth })
  const about = await drive.about.get({ fields: 'user(displayName,emailAddress)' })
  const user = (about.data && about.data.user) || {}
  const email = String(user.emailAddress || '').trim()
  if (!email) throw Object.assign(new Error('Google Drive không trả về email tài khoản'), { status: 502 })

  const creds = webCredentials()
  const tokenJson = {
    token: tokens.access_token || '',
    refresh_token: tokens.refresh_token,
    token_uri: TOKEN_URI,
    client_id: creds.clientId,
    client_secret: creds.clientSecret,
    scopes: (tokens.scope ? tokens.scope.split(' ') : SCOPES),
    universe_domain: 'googleapis.com',
  }
  if (tokens.expiry_date) tokenJson.expiry = new Date(tokens.expiry_date).toISOString()

  return {
    email,
    display_name: String(user.displayName || '').trim(),
    token_b64: Buffer.from(JSON.stringify(tokenJson)).toString('base64'),
  }
}
