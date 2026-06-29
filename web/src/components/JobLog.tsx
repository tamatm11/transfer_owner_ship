import { CheckCircle2, Circle, CircleStop, ExternalLink, Terminal, X } from 'lucide-react'
import type { Job } from '../types'

const textOf = (entry: NonNullable<Job['logs']>[number]) => typeof entry === 'string' ? entry : entry.message || JSON.stringify(entry)

export function JobLog({ job, open, onClose, onStop }: { job?: Job; open: boolean; onClose: () => void; onStop: () => void }) {
  const progress = Math.max(0, Math.min(100, job?.progress ?? (job?.status === 'completed' ? 100 : 0)))
  return <aside className={`job-log ${open ? 'open' : ''}`}>
    <div className="log-head"><div><Terminal size={19} /><strong>Nhật ký hoạt động</strong></div><div>{job && ['queued', 'running'].includes(job.status || '') && <button className="stop-button" onClick={onStop}><CircleStop size={17} />Dừng</button>}<button className="close-log" onClick={onClose} aria-label="Đóng"><X size={20} /></button></div></div>
    <div className="job-summary">{job ? <><strong>Job hiện tại · {job.type || 'Đang xử lý'}</strong><span className={`job-status ${job.status}`}>{job.status}</span><div className="progress"><i style={{ width: `${progress}%` }} /></div><small>{progress}% hoàn tất</small>{job.run_url && <a className="run-link" href={job.run_url} target="_blank" rel="noreferrer"><ExternalLink size={14} />Xem log trực tiếp trên GitHub</a>}</> : <div className="empty-log"><Terminal size={30} /><strong>Chưa có hoạt động</strong><p>Nhật ký sẽ xuất hiện khi bạn bắt đầu một tác vụ.</p></div>}</div>
    {job && <div className="log-lines">{(job.logs || []).map((entry, index) => <div className="log-line" key={index}>{job.status === 'completed' ? <CheckCircle2 size={16} /> : <Circle size={12} />}<span>{textOf(entry)}</span></div>)}{job.error && <div className="log-error">{job.error}</div>}</div>}
  </aside>
}
