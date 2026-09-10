import { useCallback, useEffect, useRef, useState } from 'react'
import * as api from './api.js'
import AnswerCard from './components/AnswerCard.jsx'
import DatasetRail from './components/DatasetRail.jsx'
import SchemaPanel from './components/SchemaPanel.jsx'
import Stages from './components/Stages.jsx'
import ThreadList from './components/ThreadList.jsx'

// Deliberately generic. Nothing here assumes a retail dataset, or any dataset.
const SUGGESTIONS = [
  'What does this dataset contain?',
  'What are the top 10 items by revenue?',
  'How did revenue change quarter over quarter?',
  'Which customers bought only once, and what share of revenue are they?',
  'Which items are most often bought together?',
]

export default function App() {
  const [datasets, setDatasets] = useState([])
  const [activeId, setActiveId] = useState(null)
  const [dataset, setDataset] = useState(null)
  const [turns, setTurns] = useState([])
  const [busy, setBusy] = useState(false)
  const [events, setEvents] = useState([])
  const [question, setQuestion] = useState('')
  const [error, setError] = useState(null)
  const [uploading, setUploading] = useState(false)
  const [uploadStage, setUploadStage] = useState('')
  const [threadId, setThreadId] = useState(null)
  const [threads, setThreads] = useState([])
  const bottom = useRef(null)

  const refreshThreads = useCallback(async (datasetId) => {
    if (!datasetId) { setThreads([]); return }
    try { setThreads(await api.listThreads(datasetId)) } catch (e) { setError(e.message) }
  }, [])

  const refresh = useCallback(async () => {
    try {
      const list = await api.listDatasets()
      setDatasets(list)
      return list
    } catch (e) { setError(e.message); return [] }
  }, [])

  useEffect(() => { refresh().then((l) => { if (l.length && !activeId) setActiveId(l[0].dataset_id) }) }, [])

  useEffect(() => {
    if (!activeId) { setDataset(null); setThreads([]); return }
    api.getDataset(activeId).then(setDataset).catch((e) => setError(e.message))
    setTurns([]); setThreadId(null); setEvents([])
    refreshThreads(activeId)
  }, [activeId, refreshThreads])

  useEffect(() => { bottom.current?.scrollIntoView({ behavior: 'smooth' }) }, [turns, events])

  async function upload(file) {
    setUploading(true); setUploadStage('Uploading…'); setError(null)
    try {
      const { job_id } = await api.uploadDataset(file, file.name.replace(/\.[^.]+$/, ''))
      api.streamJob(
        job_id,
        (e) => e.message && setUploadStage(e.message),
        async (done) => {
          setUploading(false); setUploadStage('')
          if (done.status !== 'succeeded') {
            setError(done.error?.message || 'Ingest failed.')
            return
          }
          await refresh()
          setActiveId(done.result.dataset_id)
        },
        (e) => { setUploading(false); setError(e.message) },
      )
    } catch (e) { setUploading(false); setError(e.message) }
  }

  function newChat() {
    setThreadId(null); setTurns([]); setEvents([]); setError(null)
  }

  async function openThread(id) {
    if (busy) return
    setError(null)
    try {
      const turnsFromServer = await api.getThread(id)
      setTurns(turnsFromServer.map((t) => ({
        question: t.question,
        answer: t.answer,
        error: t.error?.message,
      })))
      setThreadId(id)
      setEvents([])
    } catch (e) { setError(e.message) }
  }

  async function removeThread(id) {
    try {
      await api.deleteThread(id)
      if (id === threadId) newChat()
      await refreshThreads(activeId)
    } catch (e) { setError(e.message) }
  }

  async function submit(text) {
    const q = (text ?? question).trim()
    if (!q || !activeId || busy) return
    setQuestion(''); setBusy(true); setEvents([]); setError(null)
    setTurns((t) => [...t, { question: q }])

    try {
      const res = await api.ask({ dataset_id: activeId, question: q, thread_id: threadId })
      api.streamJob(
        res.job_id,
        (e) => setEvents((cur) => [...cur, e]),
        (done) => {
          setBusy(false); setEvents([])
          if (done.status === 'succeeded') {
            setTurns((t) => [...t.slice(0, -1), { question: q, answer: done.result }])
            setThreadId(done.result.thread_id)
          } else {
            setTurns((t) => [...t.slice(0, -1), { question: q, error: done.error?.message || 'The question failed.' }])
          }
          refreshThreads(activeId)
        },
        (e) => {
          setBusy(false); setEvents([])
          setTurns((t) => [...t.slice(0, -1), { question: q, error: e.message }])
        },
      )
    } catch (e) {
      setBusy(false)
      setTurns((t) => [...t.slice(0, -1), { question: q, error: e.message }])
    }
  }

  async function changeRole(column, role) {
    try {
      const { profile } = await api.setColumnRole(activeId, column, role)
      setDataset((d) => ({ ...d, profile }))
    } catch (e) { setError(e.message) }
  }

  return (
    <div className="app">
      <DatasetRail
        datasets={datasets} activeId={activeId} onSelect={setActiveId}
        onUpload={upload} uploading={uploading} uploadStage={uploadStage}
      >
        {activeId && (
          <ThreadList
            threads={threads} activeId={threadId} busy={busy}
            onOpen={openThread} onNew={newChat} onDelete={removeThread}
          />
        )}
      </DatasetRail>

      <main className="col chat">
        <div className="head">
          <h2>{dataset ? dataset.meta.name : 'No dataset selected'}</h2>
          <span className="grow" />
          {threadId && <span className="badge">continuing a conversation</span>}
        </div>

        {error && <div className="err">{error}</div>}

        <div className="scroll">
          <div className="messages">
            {turns.length === 0 && !busy && (
              <div className="empty">
                {activeId ? 'Ask a question about this dataset in plain English.'
                          : 'Load a CSV to get started.'}
              </div>
            )}
            {turns.map((t, i) => (
              <div className="turn" key={i}>
                <div className="q">{t.question}</div>
                {t.answer && <div style={{ marginTop: 10 }}><AnswerCard answer={t.answer} /></div>}
                {t.error && (
                  <div className="card error" style={{ marginTop: 10 }}>
                    <div className="card-body">{t.error}</div>
                  </div>
                )}
              </div>
            ))}
            {busy && <div className="turn"><Stages events={events} /></div>}
            <div ref={bottom} />
          </div>
        </div>

        <div className="composer">
          <form onSubmit={(e) => { e.preventDefault(); submit() }}>
            <textarea
              value={question} onChange={(e) => setQuestion(e.target.value)}
              placeholder={threadId ? 'Ask a follow-up…' : 'Ask a question about this data…'}
              disabled={!activeId || busy}
              onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit() } }}
            />
            <button className="btn" type="submit" disabled={!activeId || busy || !question.trim()}>
              {busy ? '…' : 'Ask'}
            </button>
          </form>
          {turns.length === 0 && activeId && (
            <div className="suggest">
              {SUGGESTIONS.map((s) => (
                <button key={s} disabled={busy} onClick={() => submit(s)}>{s}</button>
              ))}
            </div>
          )}
        </div>
      </main>

      <SchemaPanel dataset={dataset} onRoleChange={changeRole} />
    </div>
  )
}
