import React, { useState, useEffect, useRef } from 'react'
import { Play, RefreshCw, CheckCircle, AlertCircle, Clock } from 'lucide-react'

export default function IngestionPage() {
  const [status, setStatus] = useState(null)
  const [loading, setLoading] = useState(false)
  const pollRef = useRef(null)

  const fetchStatus = async () => {
    try {
      const resp = await fetch('/api/ingest/status')
      const data = await resp.json()
      setStatus(data)

      // Stop polling if completed or idle
      if (data.status !== 'running' && pollRef.current) {
        clearInterval(pollRef.current)
        pollRef.current = null
      }
    } catch (err) {
      console.error('Failed to fetch ingestion status:', err)
    }
  }

  useEffect(() => {
    fetchStatus()
    return () => { if (pollRef.current) clearInterval(pollRef.current) }
  }, [])

  const startIngestion = async (incremental) => {
    setLoading(true)
    try {
      const resp = await fetch('/api/ingest/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ incremental }),
      })
      const data = await resp.json()

      if (data.status === 'started' || data.status === 'already_running') {
        // Start polling
        pollRef.current = setInterval(fetchStatus, 2000)
        fetchStatus()
      }
    } catch (err) {
      console.error('Failed to start ingestion:', err)
    } finally {
      setLoading(false)
    }
  }

  const statusBadge = (s) => {
    switch (s) {
      case 'running': return <span className="badge badge-warning"><Clock size={12} /> Running</span>
      case 'completed': return <span className="badge badge-success"><CheckCircle size={12} /> Completed</span>
      case 'failed': return <span className="badge badge-error"><AlertCircle size={12} /> Failed</span>
      default: return <span className="badge badge-info">Idle</span>
    }
  }

  const progress = status && status.total_sources > 0
    ? Math.round((status.current_source / status.total_sources) * 100)
    : 0

  return (
    <div className="page">
      <h2>Ingestion</h2>

      <div style={{ display: 'flex', gap: 12, marginBottom: 24 }}>
        <button
          className="btn btn-primary"
          onClick={() => startIngestion(true)}
          disabled={loading || (status && status.status === 'running')}
        >
          <Play size={14} style={{ marginRight: 6 }} />
          Incremental Ingest
        </button>
        <button
          className="btn"
          onClick={() => startIngestion(false)}
          disabled={loading || (status && status.status === 'running')}
        >
          <RefreshCw size={14} style={{ marginRight: 6 }} />
          Full Re-ingest
        </button>
      </div>

      {status && (
        <div style={{ display: 'grid', gap: 16, gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))' }}>
          <div className="card">
            <h3>Status</h3>
            <div style={{ marginBottom: 12 }}>{statusBadge(status.status)}</div>
            {status.status === 'running' && (
              <>
                <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginBottom: 8 }}>
                  Source {status.current_source} of {status.total_sources}: {status.current_source_name}
                </div>
                <div className="progress-bar">
                  <div className="progress-fill" style={{ width: `${progress}%` }} />
                </div>
              </>
            )}
            {status.started_at && (
              <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 8 }}>
                Started: {new Date(status.started_at).toLocaleString()}
              </div>
            )}
            {status.completed_at && (
              <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 2 }}>
                Completed: {new Date(status.completed_at).toLocaleString()}
              </div>
            )}
          </div>

          <div className="card">
            <h3>Messages</h3>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
              <div>
                <div style={{ fontSize: 24, fontFamily: 'var(--font-mono)', fontWeight: 600 }}>
                  {status.messages_processed.toLocaleString()}
                </div>
                <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>Processed</div>
              </div>
              <div>
                <div style={{ fontSize: 24, fontFamily: 'var(--font-mono)', fontWeight: 600, color: 'var(--success)' }}>
                  {status.messages_new.toLocaleString()}
                </div>
                <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>New</div>
              </div>
              <div>
                <div style={{ fontSize: 24, fontFamily: 'var(--font-mono)', fontWeight: 600, color: 'var(--text-muted)' }}>
                  {status.messages_skipped.toLocaleString()}
                </div>
                <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>Skipped</div>
              </div>
              <div>
                <div style={{ fontSize: 24, fontFamily: 'var(--font-mono)', fontWeight: 600, color: status.error_count > 0 ? 'var(--error)' : 'var(--text-muted)' }}>
                  {status.error_count}
                </div>
                <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>Errors</div>
              </div>
            </div>
          </div>

          {status.errors && status.errors.length > 0 && (
            <div className="card" style={{ gridColumn: '1 / -1' }}>
              <h3>Recent Errors</h3>
              <div style={{ maxHeight: 200, overflow: 'auto' }}>
                {status.errors.map((err, i) => (
                  <div key={i} style={{
                    fontSize: 11,
                    fontFamily: 'var(--font-mono)',
                    color: 'var(--error)',
                    padding: '4px 0',
                    borderBottom: '1px solid var(--border)',
                  }}>
                    {err}
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
