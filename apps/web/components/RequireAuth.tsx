'use client'

import { useEffect } from 'react'
import { usePathname, useRouter } from 'next/navigation'
import type { ReactNode } from 'react'

import { useAuth } from '@/components/AuthProvider'

export function RequireAuth({ children }: { children: ReactNode }): JSX.Element {
  const auth = useAuth()
  const router = useRouter()
  const pathname = usePathname()

  useEffect(() => {
    if (auth.status === 'unauthenticated' && pathname !== '/login' && pathname !== '/signup') {
      router.replace('/login')
    }
  }, [auth.status, pathname, router])

  if (auth.status !== 'authenticated') {
    return (
      <main className="card-grid">
        <section className="card">
          <div className="muted">Loading…</div>
        </section>
      </main>
    )
  }

  return <>{children}</>
}
