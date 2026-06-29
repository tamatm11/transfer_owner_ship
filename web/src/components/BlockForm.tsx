import { Ban, CirclePlay } from 'lucide-react'
import { useState } from 'react'
import { Panel, Toggle } from './Controls'

export function BlockForm({ ownerEmail, busy, onSubmit }: { ownerEmail: string; busy: boolean; onSubmit: (payload: unknown) => void }) {
  const [folders, setFolders] = useState('')
  const [recursive, setRecursive] = useState(true)
  const [unblock, setUnblock] = useState(false)
  const [dryRun, setDryRun] = useState(true)
  return <div className="screen-form narrow">
    <div className="screen-heading"><div><h1>Chặn tải xuống</h1><p>Quản lý quyền tải xuống, sao chép và in nội dung.</p></div><span className="heading-icon"><Ban size={24} /></span></div>
    <Panel className="block-panel">
      <label className="field-label">Folder URLs / IDs</label>
      <textarea className="large-input" placeholder="Nhập mỗi URL hoặc ID trên một dòng" value={folders} onChange={e => setFolders(e.target.value)} />
      <div className="settings-panel compact">
        <Toggle label="Quét thư mục con" checked={recursive} onChange={setRecursive} />
        <Toggle label="Cho phép tải lại" checked={unblock} onChange={setUnblock} />
        <Toggle label="Chạy thử" checked={dryRun} onChange={setDryRun} />
      </div>
      {!dryRun && <p className="risk-notice">Chế độ áp dụng thật đang bật. Thay đổi sẽ được ghi vào Google Drive.</p>}
    </Panel>
    <button className="primary-action" disabled={busy || !ownerEmail || !folders.trim()} onClick={() => onSubmit({ owner_email: ownerEmail, folders: folders.split(/\r?\n|,/).map(v => v.trim()).filter(Boolean), recursive, unblock, dry_run: dryRun })}><CirclePlay size={19} />{busy ? 'Đang xử lý…' : dryRun ? 'Chạy thử' : unblock ? 'Áp dụng cho phép tải' : 'Áp dụng chặn tải'}</button>
  </div>
}
