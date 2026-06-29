import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from './api'
import { Accounts } from './components/Accounts'
import { BlockForm } from './components/BlockForm'
import { History } from './components/History'
import { JobLog } from './components/JobLog'
import { LoginScreen } from './components/LoginScreen'
import { Shell } from './components/Shell'
import { TransferForm } from './components/TransferForm'
import type { Account, Job, Role, View } from './types'

export default function App() {
  const [view, setView] = useState<View>('transfer')
  const [accounts, setAccounts] = useState<Account[]>([])
  const [connected, setConnected] = useState(false)
  const [loading, setLoading] = useState(false)
  const [job, setJob] = useState<Job>()
  const [jobs, setJobs] = useState<Job[]>([])
  const [logsOpen, setLogsOpen] = useState(false)
  const [notice, setNotice] = useState('')
  const [userEmail, setUserEmail] = useState('')
  const [authReady, setAuthReady] = useState(false)
  const activeA = useMemo(() => accounts.find(a => a.role === 'A' && a.active) || accounts.find(a => a.role === 'A'), [accounts])

  const refreshAccounts = useCallback(async () => { try { setAccounts(await api.accounts()) } catch (error) { setNotice((error as Error).message) } }, [])
  useEffect(() => {
    api.health().then(() => setConnected(true)).catch(() => setConnected(false))
    api.session()
      .then(session => {
        if (session.authenticated && session.user?.email) {
          setUserEmail(session.user.email)
          refreshAccounts()
        }
      })
      .catch(() => setUserEmail(''))
      .finally(() => setAuthReady(true))
  }, [refreshAccounts])
  useEffect(() => {
    if (!job?.id || !['queued', 'running'].includes(job.status || '')) return
    const timer = window.setInterval(async () => { try { const next = await api.job(job.id); setJob(prev => prev ? { ...prev, ...next } : next); setJobs(all => all.map(item => item.id === next.id ? { ...item, ...next } : item)) } catch (error) { setNotice((error as Error).message) } }, 1500)
    return () => window.clearInterval(timer)
  }, [job?.id, job?.status])

  const startJob = async (kind: 'transfer' | 'block', payload: unknown) => { setLoading(true); setNotice(''); try { const next = kind === 'transfer' ? await api.startTransfer(payload) : await api.startBlock(payload); const normalized = { ...next, type: next.type || kind }; setJob(normalized); setJobs(all => [normalized, ...all.filter(item => item.id !== normalized.id)]); setLogsOpen(true) } catch (error) { setNotice((error as Error).message) } finally { setLoading(false) } }
  const connectAccount = async (role: Role) => {
    const authWindow = window.open('', '_blank')
    setLoading(true)
    setNotice('')
    try {
      const result = await api.oauth(role)
      const id = result.id || result.oauth_id
      const url = result.url || result.authorization_url
      if (url) {
        if (authWindow && !authWindow.closed) {
          try { authWindow.opener = null } catch {}
          authWindow.location.href = url
        } else {
          const opened = window.open(url, '_blank', 'noopener,noreferrer')
          if (!opened) window.location.assign(url)
        }
      } else {
        authWindow?.close()
      }
      if (id) {
        const timer = window.setInterval(async () => {
          const status = await api.oauthStatus(id).catch(() => null)
          if (status && ['completed', 'success', 'succeeded', 'connected'].includes(String(status.status))) {
            window.clearInterval(timer)
            refreshAccounts()
          } else if (status?.status === 'failed') {
            window.clearInterval(timer)
            setNotice(String(status.error || status.message || 'Không thể kết nối tài khoản'))
          }
        }, 1000)
        window.setTimeout(() => window.clearInterval(timer), 120000)
      }
    } catch (error) {
      authWindow?.close()
      setNotice((error as Error).message)
    } finally {
      setLoading(false)
    }
  }
  const activate = async (account: Account) => { try { await api.activate(account.role, account.email); await refreshAccounts() } catch (error) { setNotice((error as Error).message) } }
  const stop = async () => { if (!job?.id) return; try { const next = await api.stop(job.id); setJob(next); setJobs(all => all.map(item => item.id === next.id ? next : item)) } catch (error) { setNotice((error as Error).message) } }
  const login = async (password: string) => { setLoading(true); setNotice(''); try { const result = await api.login(password); setUserEmail(result.user.email); await refreshAccounts() } catch (error) { setNotice((error as Error).message) } finally { setLoading(false) } }
  const logout = async () => { try { await api.logout() } finally { setUserEmail(''); setAccounts([]); setJob(undefined); setJobs([]); setLogsOpen(false) } }

  if (!authReady) return <div className="boot-screen">Đang mở Owner Tool…</div>
  if (!userEmail) return <LoginScreen busy={loading} notice={notice} onClearNotice={() => setNotice('')} onLogin={login} />

  return <Shell view={view} onView={setView} account={activeA} accountsA={accounts.filter(a => a.role === 'A')} onAccount={activate} connected={connected} logsOpen={logsOpen} onLogs={() => setLogsOpen(v => !v)} userEmail={userEmail} onLogout={logout}>
    <div className={`workspace ${logsOpen ? 'logs-visible' : ''}`}>
      <div className="content-area">
        {notice && <div className="notice" role="alert"><span>{notice}</span><button onClick={() => setNotice('')}>×</button></div>}
        {view === 'transfer' && <TransferForm accounts={accounts} ownerEmail={activeA?.email || ''} busy={loading} onSubmit={payload => startJob('transfer', payload)} />}
        {view === 'block' && <BlockForm ownerEmail={activeA?.email || ''} busy={loading} onSubmit={payload => startJob('block', payload)} />}
        {view === 'accounts' && <Accounts accounts={accounts} loading={loading} onConnect={connectAccount} onActivate={activate} />}
        {view === 'history' && <History jobs={jobs} onSelect={selected => { setJob(selected); setLogsOpen(true) }} />}
      </div>
      <div className="log-backdrop" onClick={() => setLogsOpen(false)} />
      <JobLog job={job} open={logsOpen} onClose={() => setLogsOpen(false)} onStop={stop} />
    </div>
  </Shell>
}
