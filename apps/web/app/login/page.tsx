'use client'

import { FormEvent, useEffect, useState } from 'react'
import Link from 'next/link'
import { useRouter } from 'next/navigation'

import { login } from '@/lib/api'
import { useAuth } from '@/components/AuthProvider'

export default function LoginPage(): JSX.Element {
  const router = useRouter()
  const auth = useAuth()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  useEffect(() => {
    if (auth.status === 'authenticated') {
      router.replace('/')
    }
  }, [auth.status, router])

  if (auth.status === 'loading') {
    return (
      <main className="card-grid">
        <section className="card">
          <div className="muted">Loading account…</div>
        </section>
      </main>
    )
  }

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    setSubmitting(true)
    try {
      setError(null)
      const response = await login({ email, password })
      auth.setSession({ token: response.access_token, user: response.user })
      router.replace('/')
    } catch (error_) {
      setError(error_ instanceof Error ? error_.message : 'failed to log in')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <main className="card-grid auth-shell">
      <section className="card stack auth-card">
        <h1 className="section-title">Log in</h1>
        <div className="muted">Use your Furnace account to continue.</div>
        {error ? <div className="badge badge-error">{error}</div> : null}
        <form className="form-grid" onSubmit={handleSubmit}>
          <label>
            Email
            <input
              type="email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              autoComplete="email"
            />
          </label>
          <label>
            Password
            <input
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              autoComplete="current-password"
            />
          </label>
          <div className="actions">
            <button className="btn" type="submit" disabled={submitting}>
              {submitting ? 'Signing in…' : 'Log in'}
            </button>
            <span className="muted small">
              No account? <Link href="/signup">Sign up</Link>
            </span>
          </div>
        </form>
      </section>
    </main>
  )
}
