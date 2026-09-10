import { useRef, useState } from 'react'

export default function DatasetRail({ datasets, activeId, onSelect, onUpload, uploading, uploadStage, children }) {
  const [over, setOver] = useState(false)
  const input = useRef(null)

  const take = (files) => { if (files?.[0]) onUpload(files[0]) }

  return (
    <aside className="col rail">
      <div className="head"><h1>Insights Engine</h1></div>

      <div
        className={`drop${over ? ' over' : ''}`}
        onDragOver={(e) => { e.preventDefault(); setOver(true) }}
        onDragLeave={() => setOver(false)}
        onDrop={(e) => { e.preventDefault(); setOver(false); take(e.dataTransfer.files) }}
      >
        {uploading ? (
          <><span className="spinner" /> <div style={{ marginTop: 8 }}>{uploadStage || 'Uploading…'}</div></>
        ) : (
          <>Drop a CSV here<br />or <label htmlFor="csv">choose a file</label></>
        )}
        <input id="csv" ref={input} type="file" accept=".csv,.tsv,.txt"
               onChange={(e) => { take(e.target.files); e.target.value = '' }} />
      </div>

      <div className="head" style={{ borderTop: '1px solid var(--line)' }}><h2>Datasets</h2></div>
      <div className="scroll rail-datasets">
        {datasets.length === 0 && <div className="empty">No datasets yet.</div>}
        {datasets.map((d) => (
          <button key={d.dataset_id} className="ds" aria-current={d.dataset_id === activeId}
                  onClick={() => onSelect(d.dataset_id)}>
            <div className="ds-name">{d.name}</div>
            <div className="ds-meta">
              {d.row_count.toLocaleString()} rows · {d.column_count} cols
            </div>
          </button>
        ))}
      </div>

      {children}
    </aside>
  )
}
