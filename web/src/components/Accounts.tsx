import { Check, KeyRound, Plus, RefreshCw } from 'lucide-react'
import type { Account, Role } from '../types'
import { Panel } from './Controls'

export function Accounts({ accounts, onConnect, onActivate, loading }: { accounts: Account[]; onConnect: (role: Role) => void; onActivate: (account: Account) => void; loading: boolean }) {
  return <div className="screen-form narrow"><div className="screen-heading"><div><h1>Quản lý tài khoản</h1><p>Kết nối và chọn tài khoản dùng cho mỗi vai trò.</p></div></div>
    {(['A', 'B'] as Role[]).map(role => <Panel className="account-group" key={role}><div className="group-title"><span className={`role-badge role-${role.toLowerCase()}`}>{role}</span><div><strong>Account {role}</strong><small>{role === 'A' ? 'Tài khoản nguồn' : 'Tài khoản nhận ownership'}</small></div><button className="secondary-action" disabled={loading} onClick={() => onConnect(role)}><Plus size={17} />Kết nối</button></div>
      <div className="account-list">{accounts.filter(a => a.role === role).length === 0 ? <div className="empty-account"><KeyRound size={20} />Chưa có tài khoản</div> : accounts.filter(a => a.role === role).map(account => <button key={account.email} className={`account-item ${account.active ? 'selected' : ''}`} onClick={() => onActivate(account)}><span className="avatar">{account.email.slice(0, 2).toUpperCase()}</span><span>{account.email}</span>{account.active ? <><Check size={18} /><small>Đang dùng</small></> : <RefreshCw size={17} />}</button>)}</div>
    </Panel>)}
  </div>
}
