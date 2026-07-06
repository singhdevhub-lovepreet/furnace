'use client'

import Link from 'next/link'

import { useAuth } from '@/components/AuthProvider'

export function AppHeader(): JSX.Element {
  const auth = useAuth()

  return (
    <header className="app-shell">
      <div className="app-shell-inner">
        <div>
          <div className="brand">Furnace</div>
          <div className="muted small">Session console</div>
        </div>
        <div className="actions" style={{ justifyContent: 'flex-end' }}>
          {auth.user ? <div className="muted small">{auth.user.email}</div> : null}
          {auth.user ? (
            <button className="btn secondary" type="button" onClick={auth.logout}>
              Logout
            </button>
          ) : null}
        </div>
        <nav className="nav">
          <Link href="/">Sessions</Link>
          <Link href="/keys">BYOK keys</Link>
        </nav>
      </div>
    </header>
  )
}
