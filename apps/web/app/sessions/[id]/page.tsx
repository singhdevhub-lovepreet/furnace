'use client'

import { useEffect, useState } from 'react'
import Link from 'next/link'
import { useParams } from 'next/navigation'

import {
  ArtifactOut,
  canCancel,
  cancelSession,
  getSession,
  getSessionUsage,
  listSessionArtifacts,
  SessionOut,
  UsageOut,
  sessionArtifactContentUrl,
} from '@/lib/api'
import { useAuth } from '@/components/AuthProvider'
import { useSessionEvents } from '@/hooks/useSessionEvents'
import { StatusBadge } from '@/components/StatusBadge'

function formatDate(value: string | null): string {
  return value ? new Date(value).toLocaleString() : '—'
}

function artifactContentElement(sessionId: string, artifact: ArtifactOut): JSX.Element {
  const url = sessionArtifactContentUrl(sessionId, artifact.id)
  if (artifact.kind === 'video') {
    return <video controls src={url} />
  }
  // eslint-disable-next-line @next/next/no-img-element
  return <img src={url} alt={artifact.kind} />
}

function artifactLabel(artifact: ArtifactOut): string {
  const filename = artifact.meta['filename']
  return typeof filename === 'string' ? filename : artifact.id
}

export default function SessionPage(): JSX.Element {
  const auth = useAuth()
  const params = useParams<{ id: string }>()
  const sessionId = params.id
  const { events, connected, error: streamError } = useSessionEvents(sessionId)
  const [session, setSession] = useState<SessionOut | null>(null)
  const [artifacts, setArtifacts] = useState<ArtifactOut[]>([])
  const [usage, setUsage] = useState<UsageOut | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (auth.status !== 'authenticated') {
      return
    }
    const load = async () => {
      try {
        setError(null)
        const [sessionResponse, artifactsResponse] = await Promise.all([
          getSession(sessionId),
          listSessionArtifacts(sessionId),
        ])
        setSession(sessionResponse)
        setArtifacts(artifactsResponse)
        try {
          setUsage(await getSessionUsage(sessionId))
        } catch (usageError) {
          if (!(usageError instanceof Error) || !usageError.message.includes('404')) {
            throw usageError
          }
          setUsage(null)
        }
      } catch (error_) {
        setError(error_ instanceof Error ? error_.message : 'failed to load session')
      } finally {
        setLoading(false)
      }
    }

    void load()
    const interval = window.setInterval(() => {
      void getSession(sessionId).then(setSession).catch(() => undefined)
    }, 5000)
    return () => {
      window.clearInterval(interval)
    }
  }, [auth.status, sessionId])

  const onCancel = async () => {
    setBusy(true)
    try {
      const updated = await cancelSession(sessionId)
      setSession(updated)
    } catch (error_) {
      setError(error_ instanceof Error ? error_.message : 'failed to cancel session')
    } finally {
      setBusy(false)
    }
  }

  if (auth.status !== 'authenticated') {
    return (
      <main className="card-grid">
        <section className="card">
          <div className="muted">Loading account…</div>
        </section>
      </main>
    )
  }

  return (
    <main className="card-grid">
      <section className="card stack">
        <div className="actions" style={{ justifyContent: 'space-between' }}>
          <div>
            <div className="muted small">
              <Link href="/">Sessions</Link> / {sessionId.slice(0, 8)}
            </div>
            <h1 className="section-title" style={{ marginBottom: 0 }}>
              Session detail
            </h1>
          </div>
          {session ? <StatusBadge status={session.status} /> : null}
        </div>
        {error ? <div className="badge badge-error">{error}</div> : null}
      </section>

      <section className="grid-2">
        <div className="card stack">
          <h2 className="section-title">Overview</h2>
          {loading || !session ? (
            <div className="muted">Loading session…</div>
          ) : (
            <div className="stack">
              <div>
                <div className="muted small">Repo</div>
                <strong>{session.repo_id}</strong>
              </div>
              <div>
                <div className="muted small">Branch</div>
                <strong>{session.branch}</strong>
              </div>
              <div>
                <div className="muted small">PR</div>
                <strong>{session.pr_number ?? '—'}</strong>
              </div>
              <div>
                <div className="muted small">Created</div>
                <strong>{formatDate(session.created_at)}</strong>
              </div>
              <div>
                <div className="muted small">Ended</div>
                <strong>{formatDate(session.ended_at)}</strong>
              </div>
              <div className="actions">
                <button
                  className="btn danger"
                  type="button"
                  disabled={!session || !canCancel(session.status) || busy}
                  onClick={onCancel}
                >
                  {busy ? 'Cancelling…' : 'Cancel'}
                </button>
                <span className="muted small">
                  Stream: {connected ? 'connected' : 'connecting'}
                </span>
              </div>
            </div>
          )}
        </div>

        <div className="card stack">
          <h2 className="section-title">Usage</h2>
          {usage ? (
            <div className="stack">
              <div className="actions" style={{ justifyContent: 'space-between' }}>
                <span className="muted">Mac seconds</span>
                <strong>{usage.mac_seconds}</strong>
              </div>
              <div className="actions" style={{ justifyContent: 'space-between' }}>
                <span className="muted">Prompt tokens</span>
                <strong>{usage.prompt_tokens}</strong>
              </div>
              <div className="actions" style={{ justifyContent: 'space-between' }}>
                <span className="muted">Completion tokens</span>
                <strong>{usage.completion_tokens}</strong>
              </div>
              <div className="actions" style={{ justifyContent: 'space-between' }}>
                <span className="muted">Cost</span>
                <strong>${usage.mac_cost_usd}</strong>
              </div>
            </div>
          ) : (
            <div className="muted">Usage not available yet.</div>
          )}
        </div>
      </section>

      <section className="grid-2">
        <div className="card stack">
          <h2 className="section-title">Live event stream</h2>
          {streamError ? <div className="badge badge-error">{streamError}</div> : null}
          <div className="muted small">Newest activity appears last.</div>
          <div className="log">
            {events.length === 0 ? (
              <div className="muted">Waiting for events…</div>
            ) : (
              events.map((event) => (
                <div className="log-entry" key={event.id}>
                  <div className="actions" style={{ justifyContent: 'space-between' }}>
                    <strong>{event.type}</strong>
                    <span className="muted small">{new Date(event.ts).toLocaleString()}</span>
                  </div>
                  <pre>{JSON.stringify(event.payload, null, 2)}</pre>
                </div>
              ))
            )}
          </div>
        </div>

        <div className="card stack">
          <h2 className="section-title">Artifacts</h2>
          {artifacts.length === 0 ? (
            <div className="muted">No artifacts yet.</div>
          ) : (
            <div className="media-grid">
              {artifacts.map((artifact) => (
                <div className="stack" key={artifact.id}>
                  <div className="actions" style={{ justifyContent: 'space-between' }}>
                    <strong>{artifact.kind}</strong>
                    <span className="muted small">{artifactLabel(artifact)}</span>
                  </div>
                  {artifactContentElement(sessionId, artifact)}
                </div>
              ))}
            </div>
          )}
        </div>
      </section>
    </main>
  )
}
