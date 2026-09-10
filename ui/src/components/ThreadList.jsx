import { useState } from 'react'

function when(iso) {
  if (!iso) return ''
  const then = new Date(iso)
  const mins = Math.round((Date.now() - then.getTime()) / 60000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  if (mins < 60 * 24) return `${Math.round(mins / 60)}h ago`
  return then.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

export default function ThreadList({ threads, activeId, onOpen, onNew, onDelete, busy }) {
  // Inline confirmation rather than window.confirm: a native dialog blocks the page and
  // there is no undo behind this button.
  const [confirming, setConfirming] = useState(null)

  return (
    <>
      <div className="head" style={{ borderTop: '1px solid var(--line)' }}>
        <h2>Chats</h2>
        <span className="grow" />
        <button className="btn ghost" onClick={onNew} disabled={busy}>New chat</button>
      </div>

      <div className="scroll">
        {threads.length === 0 && (
          <div className="empty" style={{ padding: '20px 16px' }}>
            No conversations yet.<br />Ask something to start one.
          </div>
        )}

        {threads.map((t) => (
          <div key={t.thread_id} className="thread" aria-current={t.thread_id === activeId}>
            <button className="thread-open" onClick={() => onOpen(t.thread_id)} disabled={busy}>
              <div className="thread-title">{t.title || 'Untitled conversation'}</div>
              <div className="thread-meta">
                {t.message_count} {t.message_count === 1 ? 'question' : 'questions'}
                {' · '}{when(t.updated_at)}
                {t.failed_count > 0 && <span className="thread-failed"> · {t.failed_count} failed</span>}
              </div>
            </button>

            {confirming === t.thread_id ? (
              <div className="thread-confirm">
                <button onClick={() => { onDelete(t.thread_id); setConfirming(null) }}>Delete</button>
                <button onClick={() => setConfirming(null)}>Keep</button>
              </div>
            ) : (
              <button className="thread-del" title="Delete this conversation"
                      onClick={() => setConfirming(t.thread_id)}>×</button>
            )}
          </div>
        ))}
      </div>
    </>
  )
}
