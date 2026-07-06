'use client'

import { FormEvent, useEffect, useState } from 'react'
import Link from 'next/link'
import { useRouter } from 'next/navigation'

import { signup } from '@/lib/api'
import { useAuth } from '@/components/AuthProvider'

export default function SignupPage(): JSX.Element {
  const router = useRouter()
  const auth = useAuth()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [plan, setPlan] = useState('pro')
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
      const response = await signup({ email, password, plan })
      auth.setSession({ token: response.access_token, user: response.user })
      router.replace('/')
    } catch (error_) {
      setError(error_ instanceof Error ? error_.message : 'failed to sign up')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <main className="card-grid auth-shell">
      <section className="card stack auth-card">
        <h1 className="section-title">Sign up</h1>
        <div className="muted">Create a Furnace account to get started.</div>
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
              autoComplete="new-password"
            />
          </label>
          <label>
            Plan
            <input type="text" value={plan} onChange={(event) => setPlan(event.target.value)} />
          </label>
          <div className="actions">
            <button className="btn" type="submit" disabled={submitting}>
              {submitting ? 'Creating…' : 'Create account'}
            </button>
            <span className="muted small">
              Already have an account? <Link href="/login">Log in</Link>
            </span>
          </div>
        </form>
      </section>
    </main>
  )
}
