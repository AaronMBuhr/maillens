import React from 'react'
import { BrowserRouter, Routes, Route, NavLink } from 'react-router-dom'
import { MessageSquare, Inbox, Database, Settings } from 'lucide-react'
import ChatPage from './pages/ChatPage'
import InboxPage from './pages/InboxPage'
import IngestionPage from './pages/IngestionPage'
import SettingsPage from './pages/SettingsPage'

export default function App() {
  return (
    <BrowserRouter>
      <div className="app-layout">
        <aside className="sidebar">
          <div className="sidebar-brand">
            <h1>MailLens</h1>
            <p>email intelligence</p>
          </div>
          <nav className="sidebar-nav">
            <NavLink to="/" end className={({ isActive }) => `nav-link ${isActive ? 'active' : ''}`}>
              <MessageSquare /> Query
            </NavLink>
            <NavLink to="/inbox" className={({ isActive }) => `nav-link ${isActive ? 'active' : ''}`}>
              <Inbox /> Inbox
            </NavLink>
            <NavLink to="/ingestion" className={({ isActive }) => `nav-link ${isActive ? 'active' : ''}`}>
              <Database /> Ingestion
            </NavLink>
            <NavLink to="/settings" className={({ isActive }) => `nav-link ${isActive ? 'active' : ''}`}>
              <Settings /> Settings
            </NavLink>
          </nav>
        </aside>
        <main className="main-content">
          <Routes>
            <Route path="/" element={<ChatPage />} />
            <Route path="/inbox" element={<InboxPage />} />
            <Route path="/ingestion" element={<IngestionPage />} />
            <Route path="/settings" element={<SettingsPage />} />
          </Routes>
        </main>
      </div>
    </BrowserRouter>
  )
}
