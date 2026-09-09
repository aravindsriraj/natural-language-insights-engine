// Live progress. Each entry comes from the tool that is actually running, not from a guess
// about where the agent has got to.
const LABEL = {
  stage: (e) => e.message,
  describe: (e) => `Reading column statistics${e.columns?.length ? `: ${e.columns.join(', ')}` : ''}`,
  sql_start: (e) => e.purpose || 'Running a query',
  sql_ok: (e) => `${e.purpose || 'Query'} — ${e.row_count} row(s) in ${e.elapsed_ms}ms`,
  sql_rejected: (e) => `Query rejected: ${e.reason}`,
  sql_error: (e) => `Query failed: ${e.error}`,
  refusal: (e) => `Cannot answer: ${e.reason}`,
}

export default function Stages({ events }) {
  const shown = events.filter((e) => LABEL[e.type] && e.type !== 'sql_start')
  return (
    <div className="card">
      <div className="stages">
        {shown.map((e, i) => (
          <div className={`stage${i < shown.length - 1 ? ' done' : ''}`} key={i}>
            <span className="dot" />
            <span>
              {LABEL[e.type](e)}
              {e.sql && <><br /><code>{e.sql.slice(0, 150)}{e.sql.length > 150 ? '…' : ''}</code></>}
            </span>
          </div>
        ))}
        <div className="stage"><span className="spinner" /> <span>Working…</span></div>
      </div>
    </div>
  )
}
