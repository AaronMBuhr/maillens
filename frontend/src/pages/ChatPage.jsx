import React, { useState, useRef, useEffect } from 'react'
import { Send, Filter, ChevronDown, ChevronUp, RotateCcw } from 'lucide-react'

export default function ChatPage() {
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [showFilters, setShowFilters] = useState(false)
  const [filters, setFilters] = useState({ sender: '', date_from: '', date_to: '', folder: '' })
  const [availableAccounts, setAvailableAccounts] = useState([])
  const [selectedAccounts, setSelectedAccounts] = useState(null) // null = all
  const messagesEndRef = useRef(null)
  const inputRef = useRef(null)

  useEffect(() => {
    fetch('/api/messages/accounts')
      .then(r => r.json())
      .then(data => {
        if (Array.isArray(data)) setAvailableAccounts(data)
      })
      .catch(() => {})
  }, [])

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  const allSelected = selectedAccounts === null || (availableAccounts.length > 0 && selectedAccounts?.length === availableAccounts.length)
  const noneSelected = selectedAccounts !== null && selectedAccounts.length === 0

  const toggleAccount = (acct) => {
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

  const selectAll = () => setSelectedAccounts(null)
  const selectNone = () => setSelectedAccounts([])

  const startNewConversation = () => {
    setMessages([])
    setInput('')
    inputRef.current?.focus()
  }

  const handleSubmit = async () => {
    const question = input.trim()
    if (!question || loading) return

    setInput('')
    setLoading(true)

    const prevMessages = [...messages, { role: 'user', content: question }]
    setMessages(prevMessages)

    const conversationHistory = prevMessages
      .filter(m => m.content && (m.role === 'user' || m.role === 'assistant'))
      .slice(0, -1)
      .map(m => ({ role: m.role, content: m.content }))

    const body = { question, stream: true }
    if (filters.sender) body.sender = filters.sender
    if (filters.date_from) body.date_from = filters.date_from
    if (filters.date_to) body.date_to = filters.date_to
    if (filters.folder) body.folder = filters.folder
    if (selectedAccounts !== null) body.accounts = selectedAccounts
    if (conversationHistory.length > 0) body.conversation_history = conversationHistory

    try {
      const resp = await fetch('/api/query/', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })

      if (!resp.ok) {
        throw new Error(`HTTP ${resp.status}`)
      }

      const reader = resp.body.getReader()
      const decoder = new TextDecoder()
      let assistantContent = ''
      let sources = []

      setMessages(prev => [...prev, { role: 'assistant', content: '', sources: [], status: 'Searching emails...' }])

      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        const chunk = decoder.decode(value)
        const lines = chunk.split('\n')

        for (const line of lines) {
          if (!line.startsWith('data: ')) continue
          try {
            const data = JSON.parse(line.slice(6))

            if (data.type === 'sources') {
              sources = data.sources
              setMessages(prev => {
                const updated = [...prev]
                updated[updated.length - 1] = {
                  ...updated[updated.length - 1],
                  sources,
                  status: `Analyzing ${sources.length} email${sources.length !== 1 ? 's' : ''}...`,
                }
                return updated
              })
            } else if (data.type === 'meta') {
              setMessages(prev => {
                const updated = [...prev]
                updated[updated.length - 1] = {
                  ...updated[updated.length - 1],
                  status: `Sending ${data.context_messages} emails to LLM...`,
                }
                return updated
              })
            } else if (data.type === 'text') {
              assistantContent += data.content
              setMessages(prev => {
                const updated = [...prev]
                updated[updated.length - 1] = {
                  ...updated[updated.length - 1],
                  content: assistantContent,
                  status: null,
                }
                return updated
              })
            }
          } catch (e) {
            // skip unparseable lines
          }
        }
      }
    } catch (err) {
      setMessages(prev => [
        ...prev.slice(0, -1).filter(m => m.content !== ''),
        { role: 'assistant', content: `Error: ${err.message}. Is the backend running?`, sources: [] },
      ])
    } finally {
      setLoading(false)
      inputRef.current?.focus()
    }
  }

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSubmit()
    }
  }

  return (
    <div className="chat-container">
      {messages.length === 0 ? (
        <div className="chat-empty">
          <h2>MailLens</h2>
          <p>
            Ask questions about your email archive. Try things like
            "find all emails from Jane about the Q3 budget" or
            "summarize my conversations with the recruiter last month."
          </p>
        </div>
      ) : (
        <div className="chat-messages">
          <div style={{ display: 'flex', justifyContent: 'flex-end', padding: '8px 0', position: 'sticky', top: 0, zIndex: 1 }}>
            <button className="btn" onClick={startNewConversation} style={{ gap: 6, fontSize: 12 }}>
              <RotateCcw size={13} /> New conversation
            </button>
          </div>
          {messages.map((msg, i) => (
            <div key={i} className="message">
              <div className={`message-role ${msg.role}`}>
                {msg.role === 'user' ? 'You' : 'MailLens'}
              </div>
              {msg.status && (
                <div className="message-status"><span className="spinner" /> {msg.status}</div>
              )}
              <div className="message-content">{msg.content}</div>
              {msg.sources && msg.sources.length > 0 && (
                <div className="message-sources">
                  {msg.sources.map((src, j) => (
                    <div key={j} className="source-chip">
                      <span className="source-sender">{src.sender?.split('<')[0]?.trim() || 'Unknown'}</span>
                      <span className="source-date">
                        {src.date ? new Date(src.date).toLocaleDateString() : ''}
                      </span>
                      {src.subject && <div style={{ fontSize: 10, color: 'var(--text-muted)', marginTop: 2 }}>{src.subject}</div>}
                    </div>
                  ))}
                </div>
              )}
            </div>
          ))}
          <div ref={messagesEndRef} />
        </div>
      )}

      <div className="chat-input-area">
        <button
          className="nav-link"
          onClick={() => setShowFilters(!showFilters)}
          style={{ marginBottom: 8, width: 'auto', display: 'inline-flex' }}
        >
          <Filter size={14} />
          Filters
          {showFilters ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
        </button>

        {showFilters && (
          <div className="filters-panel">
            <div className="filters-bar">
              <input
                className="filter-input"
                placeholder="Sender..."
                value={filters.sender}
                onChange={e => setFilters(f => ({ ...f, sender: e.target.value }))}
              />
              <input
                className="filter-input"
                type="date"
                placeholder="From date"
                value={filters.date_from}
                onChange={e => setFilters(f => ({ ...f, date_from: e.target.value }))}
              />
              <input
                className="filter-input"
                type="date"
                placeholder="To date"
                value={filters.date_to}
                onChange={e => setFilters(f => ({ ...f, date_to: e.target.value }))}
              />
              <input
                className="filter-input"
                placeholder="Folder..."
                value={filters.folder}
                onChange={e => setFilters(f => ({ ...f, folder: e.target.value }))}
              />
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
        )}

        <div className="chat-input-wrapper">
          <textarea
            ref={inputRef}
            className="chat-input"
            placeholder="Ask about your email..."
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            rows={1}
          />
          <button className="send-btn" onClick={handleSubmit} disabled={loading || !input.trim()}>
            <Send size={18} />
          </button>
        </div>
      </div>
    </div>
  )
}
