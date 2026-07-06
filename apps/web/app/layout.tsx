import type { ReactNode } from 'react'
import Link from 'next/link'

import './globals.css'

export const metadata = {
  title: 'Furnace',
  description: 'Live control-plane console for sessions',
}

export default function RootLayout({ children }: { children: ReactNode }): JSX.Element {
  return (
    <html lang="en">
      <body>
        <header className="app-shell">
          <div className="app-shell-inner">
            <div>
              <div className="brand">Furnace</div>
              <div className="muted small">Session console</div>
            </div>
            <nav className="nav">
              <Link href="/">Sessions</Link>
              <Link href="/keys">BYOK keys</Link>
            </nav>
          </div>
        </header>
        {children}
      </body>
    </html>
  )
}
