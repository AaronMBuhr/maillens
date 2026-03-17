import React, { useState, useEffect, useCallback } from 'react'
import { Search, ChevronLeft, ChevronRight, ChevronsLeft, ChevronsRight, ArrowUp, ArrowDown, X } from 'lucide-react'

export default function InboxPage() {
  const [messages, setMessages] = useState([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [perPage] = useState(50)
  const [loading, setLoading] = useState(false)
  const [search, setSearch] = useState({ sender: '', subject: '', folder: '' })
  const [selectedMessage, setSelectedMessage] = useState(null)
  const [availableAccounts, setAvailableAccounts] = useState([])
  const [selectedAccounts, setSelectedAccounts] = useState(null) // null = all
  const [sortBy, setSortBy] = useState('date')
  const [sortDir, setSortDir] = useState('desc')
  const [pageInput, setPageInput] = useState('')

  useEffect(() => {
    fetch('/api/messages/accounts')
      .then(r => r.json())
      .then(data => {
        if (Array.isArray(data)) setAvailableAccounts(data)
      })
      .catch(() => {})
  }, [])

  const allSelected = selectedAccounts === null || (availableAccounts.length > 0 && selectedAccounts?.length === availableAccounts.length)
  const noneSelected = selectedAccounts !== null && selectedAccounts.length === 0

  const toggleAccount = (acct) => {
    setPage(1)
    if (selectedAccounts === null) {
      setSelectedAccounts(availableAccounts.filter(a => a.account !== acct).map(a => a.account))
    } else if (selectedAccounts.includes(acct)) {
      setSelectedAccounts(selectedAccounts.filter(a => a !== acct))
    } else {
      const next = [...selectedAccounts, acct]
      if (next.length === availableAccounts.length) {
        setSelectedAccounts(null)
      } else {
        setSelectedAccounts(next)
      }
    }
  }

  const selectAll = () => { setSelectedAccounts(null); setPage(1) }
  const selectNone = () => { setSelectedAccounts([]); setPage(1) }

  const toggleSort = (col) => {
    if (sortBy === col) {
      setSortDir(d => d === 'asc' ? 'desc' : 'asc')
    } else {
      setSortBy(col)
      setSortDir(col === 'date' ? 'desc' : 'asc')
    }
    setPage(1)
  }

  const fetchMessages = useCallback(async () => {
    setLoading(true)
    const params = new URLSearchParams({ page, per_page: perPage, sort_by: sortBy, sort_dir: sortDir })
    if (search.sender) params.set('sender', search.sender)
    if (search.subject) params.set('subject', search.subject)
    if (search.folder) params.set('folder', search.folder)
    if (selectedAccounts !== null) {
      selectedAccounts.forEach(a => params.append('accounts', a))
    }

    try {
      const resp = await fetch(`/api/messages/?${params}`)
      const data = await resp.json()
      setMessages(data.messages || [])
      setTotal(data.total || 0)
    } catch (err) {
      console.error('Failed to fetch messages:', err)
    } finally {
      setLoading(false)
    }
  }, [page, perPage, search, selectedAccounts, sortBy, sortDir])

  useEffect(() => { fetchMessages() }, [fetchMessages])

  const fetchFullMessage = async (id) => {
    try {
      const resp = await fetch(`/api/messages/${id}`)
      const data = await resp.json()
      setSelectedMessage(data)
    } catch (err) {
      console.error('Failed to fetch message:', err)
    }
  }

  const totalPages = Math.ceil(total / perPage)

  return (
    <div className="page">
      <h2>Inbox Browser</h2>

      <div className="filters-panel" style={{ marginBottom: 16 }}>
        <div className="filters-bar">
          <input
            className="filter-input"
            placeholder="Search sender..."
            value={search.sender}
            onChange={e => { setSearch(s => ({ ...s, sender: e.target.value })); setPage(1) }}
          />
          <input
            className="filter-input"
            placeholder="Search subject..."
            value={search.subject}
            onChange={e => { setSearch(s => ({ ...s, subject: e.target.value })); setPage(1) }}
          />
          <input
            className="filter-input"
            placeholder="Folder..."
            value={search.folder}
            onChange={e => { setSearch(s => ({ ...s, folder: e.target.value })); setPage(1) }}
          />
          <span style={{ color: 'var(--text-muted)', fontSize: 12, alignSelf: 'center' }}>
            {total.toLocaleString()} messages
          </span>
        </div>
        {availableAccounts.length > 0 && (
          <div className="account-selector">
            <span className="account-selector-label">Accounts</span>
            <button className="account-toggle-btn" onClick={selectAll} data-active={allSelected || undefined}>All</button>
            <button className="account-toggle-btn" onClick={selectNone} data-active={noneSelected || undefined}>None</button>
            <span className="account-selector-sep" />
            {availableAccounts.map(a => {
              const checked = selectedAccounts === null || selectedAccounts.includes(a.account)
              return (
                <label key={a.account} className="account-checkbox">
                  <input
                    type="checkbox"
                    checked={checked}
                    onChange={() => toggleAccount(a.account)}
                  />
                  <span className="account-name">{a.account}</span>
                  <span className="account-count">{a.count.toLocaleString()}</span>
                </label>
              )
            })}
          </div>
        )}
      </div>

      {selectedMessage ? (
        <div className="card">
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'start', marginBottom: 16 }}>
            <div>
              <h3 style={{ fontSize: 15, color: 'var(--text-primary)' }}>{selectedMessage.subject || '(no subject)'}</h3>
              <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginTop: 4 }}>
                From: {selectedMessage.sender} &middot; {selectedMessage.date ? new Date(selectedMessage.date).toLocaleString() : 'Unknown date'}
                {selectedMessage.account && <> &middot; <span style={{ fontFamily: 'var(--font-mono)' }}>{selectedMessage.account}</span></>}
              </div>
              {selectedMessage.recipients_to && (
                <div style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 2 }}>
                  To: {selectedMessage.recipients_to}
                </div>
              )}
            </div>
            <button className="btn" onClick={() => setSelectedMessage(null)} style={{ padding: '4px 8px' }}>
              <X size={14} />
            </button>
          </div>
          <pre style={{
            fontFamily: 'var(--font-sans)',
            fontSize: 13,
            lineHeight: 1.7,
            whiteSpace: 'pre-wrap',
            wordWrap: 'break-word',
            color: 'var(--text-primary)',
          }}>
            {selectedMessage.body_clean || selectedMessage.body_text || '(no content)'}
          </pre>
        </div>
      ) : (
        <>
          <table className="data-table">
            <thead>
              <tr>
                {[
                  { key: 'date', label: 'Date' },
                  { key: 'sender', label: 'Sender' },
                  { key: 'subject', label: 'Subject' },
                  { key: 'account', label: 'Account' },
                  { key: 'folder', label: 'Folder' },
                ].map(col => (
                  <th key={col.key} className="sortable-th" onClick={() => toggleSort(col.key)}>
                    <span>{col.label}</span>
                    {sortBy === col.key && (
                      sortDir === 'asc' ? <ArrowUp size={12} /> : <ArrowDown size={12} />
                    )}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {loading ? (
                <tr><td colSpan={5} style={{ textAlign: 'center', padding: 40 }}><span className="spinner" /></td></tr>
              ) : messages.length === 0 ? (
                <tr><td colSpan={5} style={{ textAlign: 'center', padding: 40, color: 'var(--text-muted)' }}>No messages indexed yet. Run ingestion first.</td></tr>
              ) : messages.map(msg => (
                <tr key={msg.id} onClick={() => fetchFullMessage(msg.id)} style={{ cursor: 'pointer' }}>
                  <td style={{ whiteSpace: 'nowrap', width: 120 }}>
                    {msg.date ? new Date(msg.date).toLocaleDateString() : '—'}
                  </td>
                  <td style={{ maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {msg.sender?.split('<')[0]?.trim() || msg.sender || '—'}
                  </td>
                  <td>{msg.subject || '(no subject)'}</td>
                  <td style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--text-muted)' }}>
                    {msg.account || '—'}
                  </td>
                  <td style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--text-muted)' }}>
                    {msg.folder || '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          {totalPages > 1 && (
            <div className="pagination-bar">
              <button className="btn" onClick={() => setPage(1)} disabled={page === 1} title="First page">
                <ChevronsLeft size={14} /><ChevronLeft size={14} style={{ marginLeft: -8 }} />
              </button>
              <button className="btn" onClick={() => setPage(p => Math.max(1, p - 10))} disabled={page <= 1} title="Back 10 pages">
                <ChevronsLeft size={14} />
              </button>
              <button className="btn" onClick={() => setPage(p => Math.max(1, p - 1))} disabled={page === 1} title="Previous page">
                <ChevronLeft size={14} />
              </button>

              <form
                className="page-input-form"
                onSubmit={e => {
                  e.preventDefault()
                  const p = parseInt(pageInput, 10)
                  if (p >= 1 && p <= totalPages) setPage(p)
                  setPageInput('')
                }}
              >
                <span className="page-label">Page</span>
                <input
                  className="page-input"
                  type="text"
                  inputMode="numeric"
                  placeholder={String(page)}
                  value={pageInput}
                  onChange={e => setPageInput(e.target.value.replace(/\D/g, ''))}
                />
                <span className="page-label">of {totalPages.toLocaleString()}</span>
              </form>

              <button className="btn" onClick={() => setPage(p => Math.min(totalPages, p + 1))} disabled={page === totalPages} title="Next page">
                <ChevronRight size={14} />
              </button>
              <button className="btn" onClick={() => setPage(p => Math.min(totalPages, p + 10))} disabled={page >= totalPages} title="Forward 10 pages">
                <ChevronsRight size={14} />
              </button>
              <button className="btn" onClick={() => setPage(totalPages)} disabled={page === totalPages} title="Last page">
                <ChevronRight size={14} style={{ marginRight: -8 }} /><ChevronsRight size={14} />
              </button>
            </div>
          )}
        </>
      )}
    </div>
  )
}
