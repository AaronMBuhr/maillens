import React, { useState, useEffect } from 'react'
import { Save, RefreshCw } from 'lucide-react'

export default function SettingsPage() {
  const [settings, setSettings] = useState(null)
  const [stats, setStats] = useState(null)
  const [saving, setSaving] = useState(false)
  const [selectedProvider, setSelectedProvider] = useState('')

  const fetchSettings = async () => {
    try {
      const resp = await fetch('/api/settings/')
      const data = await resp.json()
      setSettings(data)
      setSelectedProvider(data.active_provider)
    } catch (err) {
      console.error('Failed to fetch settings:', err)
    }
  }

  const fetchStats = async () => {
    try {
      const resp = await fetch('/api/settings/stats')
      const data = await resp.json()
      setStats(data)
    } catch (err) {
      console.error('Failed to fetch stats:', err)
    }
  }

  useEffect(() => {
    fetchSettings()
    fetchStats()
  }, [])

  const saveProvider = async () => {
    setSaving(true)
    try {
      const resp = await fetch('/api/settings/provider', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ provider: selectedProvider }),
      })
      const data = await resp.json()
      if (data.status === 'ok') {
        setSettings(s => ({ ...s, active_provider: selectedProvider }))
      }
    } catch (err) {
      console.error('Failed to save provider:', err)
    } finally {
      setSaving(false)
    }
  }

  if (!settings) {
    return <div className="page"><span className="spinner" /></div>
  }

  return (
    <div className="page">
      <h2>Settings</h2>

      <div style={{ display: 'grid', gap: 24, maxWidth: 600 }}>
        <div className="card">
          <h3>LLM Provider</h3>
          <div className="form-group">
            <label className="form-label">Active Provider</label>
            <select
              className="form-select"
              value={selectedProvider}
              onChange={e => setSelectedProvider(e.target.value)}
            >
              {settings.available_providers.map(p => (
                <option key={p} value={p}>{p}</option>
              ))}
            </select>
          </div>

          <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginBottom: 12 }}>
            {settings.available_providers.map(name => {
              const p = settings.providers[name]
              if (!p) return null
              const isActive = name === settings.active_provider
              return (
                <div key={name} style={{
                  marginBottom: 8,
                  padding: '6px 8px',
                  borderRadius: 'var(--radius)',
                  background: isActive ? 'var(--accent-dim)' : 'transparent',
                }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <strong style={{ textTransform: 'capitalize' }}>{name}</strong>
                    <span style={{ fontFamily: 'var(--font-mono)', fontSize: 11 }}>{p.model}</span>
                    {p.has_key
                      ? <span className="badge badge-success">Key set</span>
                      : <span className="badge badge-error">No key</span>
                    }
                  </div>
                  <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 2 }}>
                    Context: {(p.max_context_tokens).toLocaleString()} tokens
                    &middot; Output: {(p.max_tokens).toLocaleString()} tokens
                    {p.url && <> &middot; {p.url}</>}
                  </div>
                </div>
              )
            })}
          </div>

          <div style={{ fontSize: 11, color: 'var(--text-muted)', marginBottom: 12 }}>
            API keys are set via environment variables or config.yaml. They cannot be changed from this UI for security.
          </div>

          {selectedProvider !== settings.active_provider && (
            <button className="btn btn-primary" onClick={saveProvider} disabled={saving}>
              <Save size={14} style={{ marginRight: 6 }} />
              {saving ? 'Saving...' : 'Switch Provider'}
            </button>
          )}
        </div>

        <div className="card">
          <h3>Retrieval Settings</h3>
          <div style={{ fontSize: 12, color: 'var(--text-secondary)' }}>
            <div>Top K: {settings.retrieval_top_k}</div>
            <div>Similarity threshold: {settings.retrieval_similarity_threshold}</div>
            <div>Embedding model: {settings.embedding_model}</div>
            <div>Mail directory: <code style={{ fontFamily: 'var(--font-mono)' }}>{settings.mail_directory}</code></div>
          </div>
          <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 8 }}>
            These settings are configured in config.yaml. Restart the container after changes.
          </div>
        </div>

        {stats && (
          <div className="card">
            <h3>
              Database Stats
              <button className="btn" onClick={fetchStats} style={{ marginLeft: 12, padding: '2px 8px' }}>
                <RefreshCw size={12} />
              </button>
            </h3>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginBottom: 12 }}>
              <div>
                <div style={{ fontSize: 24, fontFamily: 'var(--font-mono)', fontWeight: 600 }}>
                  {stats.message_count.toLocaleString()}
                </div>
                <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>Messages</div>
              </div>
              <div>
                <div style={{ fontSize: 24, fontFamily: 'var(--font-mono)', fontWeight: 600 }}>
                  {stats.folder_count}
                </div>
                <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>Folders</div>
              </div>
            </div>
            {stats.top_senders && stats.top_senders.length > 0 && (
              <div>
                <div style={{ fontSize: 11, fontWeight: 600, color: 'var(--text-muted)', marginBottom: 6 }}>Top Senders</div>
                {stats.top_senders.slice(0, 10).map((s, i) => (
                  <div key={i} style={{ fontSize: 12, color: 'var(--text-secondary)', padding: '2px 0' }}>
                    {s.sender?.split('<')[0]?.trim()} — {s.count}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
