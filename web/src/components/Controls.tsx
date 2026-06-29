import type { ReactNode } from 'react'

export function Toggle({ checked, onChange, label, disabled = false }: { checked: boolean; onChange: (value: boolean) => void; label: string; disabled?: boolean }) {
  return <label className={`toggle-row ${disabled ? 'disabled' : ''}`}><span>{label}</span><button type="button" role="switch" aria-checked={checked} aria-disabled={disabled} disabled={disabled} className={`toggle ${checked ? 'on' : ''}`} onClick={() => onChange(!checked)}><i /></button></label>
}

export function RadioGroup({ label, value, options, onChange }: { label: string; value: string; options: Array<{ value: string; label: string }>; onChange: (value: string) => void }) {
  return <div className="option-row"><strong>{label}</strong><div className="radio-group">{options.map(option => <label key={option.value}><input type="radio" name={label} checked={value === option.value} onChange={() => onChange(option.value)} /><span>{option.label}</span></label>)}</div></div>
}

export function Panel({ children, className = '' }: { children: ReactNode; className?: string }) { return <section className={`panel ${className}`}>{children}</section> }
