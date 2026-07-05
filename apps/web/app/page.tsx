'use client'

import { FormEvent, useEffect, useMemo, useState } from 'react'
import Link from 'next/link'
import { useRouter } from 'next/navigation'

import {
  buildDefaultModelPolicy,
  createSession,
  getPoolStatus,
  listModels,
  listRepos,
  listSessions,
  ModelCatalog,
  PoolStatusOut,
  ProviderName,
  RepoOut,
  SessionOut,
  SessionListRow,
} from '@/lib/api'
import { StatusBadge } from '@/components/StatusBadge'

interface NewSessionFormState {
  repoId: string
  prompt: string
  provider: ProviderName
  model: string
}

function formatDate(value: string): string {
  return new Date(value).toLocaleString()
}

function repoNameFor(session: SessionOut, repos: RepoOut[]): string {
  return repos.find((repo) => repo.id === session.repo_id)?.full_name ?? session.repo_id
}

function toListRow(session: SessionOut, repos: RepoOut[]): SessionListRow {
  return { ...session, repo_name: repoNameFor(session, repos) }
}

function firstModelForProvider(catalog: ModelCatalog, provider: ProviderName): string {
  return catalog.providers.find((item) => item.provider === provider)?.models[0] ?? ''
}

export default function HomePage(): JSX.Element {
  const router = useRouter()
  const [sessions, setSessions] = useState<SessionOut[]>([])
  const [repos, setRepos] = useState<RepoOut[]>([])
  const [catalog, setCatalog] = useState<ModelCatalog | null>(null)
  const [pool, setPool] = useState<PoolStatusOut | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [form, setForm] = useState<NewSessionFormState>({
    repoId: '',
    prompt: '',
    provider: 'openrouter',
    model: '',
  })

  const repoRows = useMemo(
    () => sessions.map((session) => toListRow(session, repos)),
    [repos, sessions],
  )

  useEffect(() => {
    const refresh = async () => {
      try {
        setError(null)
        const [sessionsResponse, reposResponse, modelsResponse, poolResponse] = await Promise.all([
          listSessions(),
          listRepos(),
          listModels(),
          getPoolStatus(),
        ])
        setSessions(sessionsResponse)
        setRepos(reposResponse)
        setCatalog(modelsResponse)
        setPool(poolResponse)
        const firstProvider = modelsResponse.providers[0]?.provider ?? 'openrouter'
        setForm((current) => {
          if (current.model) {
            return current
          }
          return {
            ...current,
            provider: firstProvider,
            model: firstModelForProvider(modelsResponse, firstProvider),
          }
        })
      } catch (error_) {
        setError(error_ instanceof Error ? error_.message : 'failed to load sessions')
      } finally {
        setLoading(false)
      }
    }
    void refresh()
    const interval = window.setInterval(() => {
      void getPoolStatus()
        .then((value) => setPool(value))
        .catch(() => undefined)
    }, 10000)
    return () => {
      window.clearInterval(interval)
    }
  }, [])

  useEffect(() => {
    if (!catalog) {
      return
    }
    const provider = form.provider
    const nextModel = firstModelForProvider(catalog, provider)
    if (nextModel && form.model !== nextModel) {
      setForm((current) => ({ ...current, model: nextModel }))
    }
  }, [catalog, form.model, form.provider])

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!form.repoId || !form.prompt.trim()) {
      return
    }
    setSubmitting(true)
    try {
      const modelPolicy = form.model
        ? buildDefaultModelPolicy(form.provider, form.model)
        : {}
      const created = await createSession({
        repo_id: form.repoId,
        prompt: form.prompt,
        model_policy: modelPolicy,
      })
      router.push(`/sessions/${created.id}`)
    } catch (error_) {
      setError(error_ instanceof Error ? error_.message : 'failed to create session')
    } finally {
      setSubmitting(false)
    }
  }

  const providerOptions = catalog?.providers ?? []
  const selectedProviderModels =
    providerOptions.find((item) => item.provider === form.provider)?.models ?? []

  return (
    <main className="card-grid">
      <section className="card stack">
        <div className="actions" style={{ justifyContent: 'space-between' }}>
          <div>
            <h1 className="section-title">Sessions</h1>
            <div className="muted">Create sessions and watch them progress live.</div>
          </div>
          {pool ? (
            <div className="card" style={{ padding: '0.75rem 1rem' }}>
              <div className="small muted">Pool</div>
              <strong>
                {pool.active_sessions}/{pool.capacity} active
              </strong>
              <div className="small muted">Queue: {pool.queue_depth}</div>
            </div>
          ) : null}
        </div>
        {error ? <div className="badge badge-error">{error}</div> : null}
      </section>

      <section className="grid-2">
        <div className="card stack">
          <h2 className="section-title">New Session</h2>
          <form className="form-grid" onSubmit={handleSubmit}>
            <label>
              Repo
              <select
                value={form.repoId}
                onChange={(event) => setForm((current) => ({ ...current, repoId: event.target.value }))}
              >
                <option value="">Select a repo</option>
                {repos.map((repo) => (
                  <option key={repo.id} value={repo.id}>
                    {repo.full_name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Prompt
              <textarea
                value={form.prompt}
                onChange={(event) => setForm((current) => ({ ...current, prompt: event.target.value }))}
                placeholder="Describe the change you want..."
              />
            </label>
            <label>
              Provider
              <select
                value={form.provider}
                onChange={(event) => {
                  const provider = event.target.value as ProviderName
                  const nextModel = firstModelForProvider(catalog ?? { providers: [] }, provider)
                  setForm((current) => ({
                    ...current,
                    provider,
                    model: nextModel,
                  }))
                }}
              >
                {providerOptions.map((provider) => (
                  <option key={provider.provider} value={provider.provider}>
                    {provider.provider}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Model
              <select
                value={form.model}
                onChange={(event) => setForm((current) => ({ ...current, model: event.target.value }))}
              >
                <option value="">Use default model policy</option>
                {selectedProviderModels.map((model) => (
                  <option key={model} value={model}>
                    {model}
                  </option>
                ))}
              </select>
            </label>
            <div className="actions">
              <button className="btn" type="submit" disabled={submitting}>
                {submitting ? 'Creating…' : 'Create session'}
              </button>
              <span className="muted small">Empty model policy `{}` remains valid.</span>
            </div>
          </form>
        </div>

        <div className="card stack">
          <h2 className="section-title">Pool status</h2>
          {pool ? (
            <div className="stack">
              <div className="actions" style={{ justifyContent: 'space-between' }}>
                <span className="muted">Active</span>
                <strong>
                  {pool.active_sessions}/{pool.capacity}
                </strong>
              </div>
              <div className="actions" style={{ justifyContent: 'space-between' }}>
                <span className="muted">Queue</span>
                <strong>{pool.queue_depth}</strong>
              </div>
              <div className="actions" style={{ justifyContent: 'space-between' }}>
                <span className="muted">Scale up</span>
                <strong>{pool.scale_decision.scale_up_by}</strong>
              </div>
            </div>
          ) : (
            <div className="muted">Loading pool status…</div>
          )}
        </div>
      </section>

      <section className="card stack">
        <div className="actions" style={{ justifyContent: 'space-between' }}>
          <h2 className="section-title" style={{ margin: 0 }}>
            Recent sessions
          </h2>
          <div className="muted small">Most recent first</div>
        </div>
        {loading ? (
          <div className="muted">Loading sessions…</div>
        ) : repoRows.length === 0 ? (
          <div className="muted">No sessions yet.</div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Repo</th>
                  <th>Status</th>
                  <th>Prompt</th>
                  <th>Created</th>
                  <th>PR</th>
                </tr>
              </thead>
              <tbody>
                {repoRows.map((session) => (
                  <tr key={session.id}>
                    <td>
                      <Link href={`/sessions/${session.id}`}>{session.id.slice(0, 8)}</Link>
                    </td>
                    <td>{session.repo_name}</td>
                    <td>
                      <StatusBadge status={session.status} />
                    </td>
                    <td>{session.prompt}</td>
                    <td>{formatDate(session.created_at)}</td>
                    <td>{session.pr_number ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </main>
  )
}
