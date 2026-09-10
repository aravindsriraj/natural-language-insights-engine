// Thin client over the HTTP API. Every long operation is a job: submit, then stream.

async function request(path, options = {}) {
  const res = await fetch(path, options)
  if (res.status === 204) return null
  const body = await res.json().catch(() => null)
  if (!res.ok) {
    const err = body?.error ?? { code: 'unknown', message: `HTTP ${res.status}` }
    const detail = err.fields?.map((f) => `${f.field}: ${f.problem}`).join('; ')
    throw Object.assign(new Error(detail ? `${err.message} (${detail})` : err.message), { code: err.code })
  }
  return body
}

export const health = () => request('/health')
export const listDatasets = () => request('/api/datasets').then((d) => d.datasets)
export const getDataset = (id) => request(`/api/datasets/${id}`)
export const deleteDataset = (id) => request(`/api/datasets/${id}`, { method: 'DELETE' })
export const setColumnRole = (id, column, role) =>
  request(`/api/datasets/${id}/columns/${encodeURIComponent(column)}`, {
    method: 'PATCH',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ role }),
  })

export function uploadDataset(file, name) {
  const form = new FormData()
  form.append('file', file)
  const qs = name ? `?name=${encodeURIComponent(name)}` : ''
  return request(`/api/datasets${qs}`, { method: 'POST', body: form })
}

export const ask = (body) =>
  request('/api/query', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  })

export const listThreads = (datasetId) =>
  request(`/api/threads${datasetId ? `?dataset_id=${datasetId}` : ''}`).then((d) => d.threads)
export const getThread = (id) => request(`/api/threads/${id}`).then((d) => d.turns)
export const deleteThread = (id) => request(`/api/threads/${id}`, { method: 'DELETE' })

export const clearCache = (datasetId) =>
  request(`/api/cache${datasetId ? `?dataset_id=${datasetId}` : ''}`, { method: 'DELETE' })

// Server-sent events for one job. Returns an unsubscribe function.
export function streamJob(jobId, onEvent, onDone, onError) {
  const source = new EventSource(`/api/jobs/${jobId}/events`)
  source.onmessage = (msg) => {
    let event
    try { event = JSON.parse(msg.data) } catch { return }
    if (event.type === 'done') {
      source.close()
      onDone?.(event)
    } else {
      onEvent?.(event)
    }
  }
  source.onerror = () => { source.close(); onError?.(new Error('Connection to the server was lost.')) }
  return () => source.close()
}
