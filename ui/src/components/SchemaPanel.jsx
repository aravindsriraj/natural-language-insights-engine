const ROLES = ['date', 'money', 'quantity', 'identifier', 'category', 'boolean', 'text', 'other']

function pct(v) { return `${(v * 100).toFixed(v > 0 && v < 0.01 ? 2 : 0)}%` }

export default function SchemaPanel({ dataset, onRoleChange }) {
  if (!dataset) {
    return (
      <aside className="col schema">
        <div className="head"><h2>Schema</h2></div>
        <div className="empty">Select a dataset to see how its schema was understood.</div>
      </aside>
    )
  }

  const { profile, meta } = dataset
  const sem = profile.semantics

  return (
    <aside className="col schema">
      <div className="head">
        <h2>Schema</h2>
        <span className="grow" />
        <span className="badge">{profile.row_count.toLocaleString()} rows</span>
      </div>

      <div className="scroll">
        {sem ? (
          <div className="sem">
            {sem.grain && <div><b>Grain.</b> {sem.grain}</div>}
            {sem.revenue_expression && (
              <div><b>Value per row.</b> <code>{sem.revenue_expression}</code></div>
            )}
            {sem.time_column && <div><b>Time.</b> <code>{sem.time_column}</code></div>}
            {sem.has_returns && <div className="caveat"><b>Returns present.</b> {sem.returns_note}</div>}
            {(sem.caveats || []).map((c, i) => (
              <div key={i} className="caveat">⚠ {c}</div>
            ))}
          </div>
        ) : (
          <div className="sem caveat">
            Semantic annotation unavailable{profile.semantics_error ? ` (${profile.semantics_error})` : ''}.
            Column statistics below are still exact.
          </div>
        )}

        <div className="head" style={{ borderTop: '1px solid var(--line)' }}>
          <h2>{profile.column_count} columns</h2>
          <span className="grow" />
          <span className="badge" title="How the file was decoded">{meta.read_options}</span>
        </div>

        {profile.columns.map((c) => (
          <div className="col-row" key={c.name}>
            <div className="col-name">
              {c.name}
              <span className="col-type">{c.type}</span>
              <span className="grow" />
              <select className="role" value={c.role || 'other'} title="Correct the inferred role"
                      onChange={(e) => onRoleChange(c.name, e.target.value)}>
                {ROLES.map((r) => <option key={r} value={r}>{r}</option>)}
              </select>
            </div>
            {c.description && <div className="col-desc">{c.description}</div>}
            <div className="col-stats">
              {c.distinct_count?.toLocaleString()} distinct
              {c.null_rate > 0 && ` · ${pct(c.null_rate)} null`}
              {c.has_negatives && ' · has negatives'}
              {c.unique && ' · unique'}
            </div>
          </div>
        ))}
      </div>
    </aside>
  )
}
