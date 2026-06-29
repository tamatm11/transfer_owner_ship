import { LockKeyhole, ShieldCheck } from 'lucide-react'
import { FormEvent, useState } from 'react'

export function LoginScreen({ busy, notice, onClearNotice, onLogin }: { busy: boolean; notice: string; onClearNotice: () => void; onLogin: (password: string) => void }) {
  const [password, setPassword] = useState('')
  const submit = (event: FormEvent) => {
    event.preventDefault()
    onLogin(password)
  }

  return <main className="login-shell">
    {notice && <div className="notice login-notice" role="alert"><span>{notice}</span><button onClick={onClearNotice}>×</button></div>}
    <section className="login-card">
      <div className="login-mark"><ShieldCheck size={26} /></div>
      <p className="eyebrow">Private Drive Ops</p>
      <h1>Đăng nhập Owner Video Tool</h1>
      <p className="login-copy">Web app này chỉ mở cho đúng tài khoản được chỉ định. Mật khẩu do anh tự tạo trong biến môi trường Vercel.</p>
      <form onSubmit={submit}>
        <label>
          <span>Mật khẩu</span>
          <input type="password" value={password} onChange={event => setPassword(event.target.value)} placeholder="Nhập mật khẩu anh đã đặt" autoComplete="current-password" />
        </label>
        <button className="primary-action login-button" disabled={busy || !password}><LockKeyhole size={18} />{busy ? 'Đang kiểm tra…' : 'Đăng nhập'}</button>
      </form>
    </section>
  </main>
}
