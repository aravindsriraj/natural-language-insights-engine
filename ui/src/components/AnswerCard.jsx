import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import ResultChart from './ResultChart.jsx'

const fmt = (v) =>
  v === null || v === undefined ? <span style={{ color: 'var(--faint)' }}>NULL</span> : String(v)

function ResultTable({ result }) {
  if (!result?.rows?.length) return null
  const { columns, rows } = result
  return (
    <div className="rows">
      <table>
        <thead><tr>{columns.map((c) => <th key={c}>{c}</th>)}</tr></thead>
        <tbody>
          {rows.slice(0, 100).map((r, i) => (
            <tr key={i}>
              {r.map((v, j) => (
                <td key={j} className={typeof v === 'number' ? 'num' : ''}>{fmt(v)}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default function AnswerCard({ answer }) {
  const queries = answer.queries || []
  const refused = answer.refused

  return (
    <div className={`card${refused ? ' refused' : ''}`}>
      <div className="card-body">
        <Markdown remarkPlugins={[remarkGfm]}>{answer.answer}</Markdown>
      </div>

      {!refused && <ResultChart chart={answer.chart} result={answer.result} />}
      {!refused && <ResultTable result={answer.result} />}

      {refused && answer.missing_concepts?.length > 0 && (
        <div className="note">
          <strong>This dataset would need:</strong>
          <ul>{answer.missing_concepts.map((m, i) => <li key={i}>{m}</li>)}</ul>
        </div>
      )}

      {answer.clarification && (
        <div className="note"><strong>To be sure:</strong> {answer.clarification}</div>
      )}

      {answer.assumptions?.length > 0 && (
        <div className="note">
          <strong>Assumptions</strong>
          <ul>{answer.assumptions.map((a, i) => <li key={i}>{a}</li>)}</ul>
        </div>
      )}

      {queries.length > 0 && (
        <details className="sql">
          <summary>
            {queries.length === 1 ? 'Show the query that produced this'
              : `Show the ${queries.length} queries that produced this`}
          </summary>
          {queries.map((q, i) => (
            <div key={i}>
              {q.purpose && <div className="sql-purpose">{q.purpose}</div>}
              <pre>{q.sql}</pre>
              <div className="sql-purpose" style={{ paddingBottom: 8 }}>
                {q.row_count} row(s) in {q.elapsed_ms}ms{q.truncated ? ' · truncated' : ''}
              </div>
            </div>
          ))}
        </details>
      )}

      <div className="badges">
        <span className={`badge ${answer.confidence}`}>{answer.confidence} confidence</span>
        {refused && <span className="badge medium">refused</span>}
        {queries.length > 0 && <span className="badge">{queries.length} quer{queries.length === 1 ? 'y' : 'ies'}</span>}
        {answer.usage?.total_tokens > 0 && (
          <span className="badge">{answer.usage.total_tokens.toLocaleString()} tokens</span>
        )}
        {answer.elapsed_ms > 0 && <span className="badge">{(answer.elapsed_ms / 1000).toFixed(1)}s</span>}
      </div>
    </div>
  )
}
