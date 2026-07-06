'use client'

import { FormEvent, useEffect, useState } from 'react'

import {
  createKey,
  deleteKey,
  listKeys,
  listModels,
  LlmKeyOut,
  ModelCatalog,
  ProviderName,
} from '@/lib/api'
import { useAuth } from '@/components/AuthProvider'

interface KeyFormState {
  provider: ProviderName
  label: string
  key: string
}

export default function KeysPage(): JSX.Element {
  const auth = useAuth()
  const [keys, setKeys] = useState<LlmKeyOut[]>([])
  const [catalog, setCatalog] = useState<ModelCatalog | null>(null)
  const [form, setForm] = useState<KeyFormState>({ provider: 'openrouter', label: '', key: '' })
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  useEffect(() => {
    if (auth.status !== 'authenticated') {
      return
    }
    const load = async () => {
      try {
        setError(null)
        const [keysResponse, catalogResponse] = await Promise.all([listKeys(), listModels()])
        setKeys(keysResponse)
        setCatalog(catalogResponse)
      } catch (error_) {
        setError(error_ instanceof Error ? error_.message : 'failed to load keys')
      } finally {
        setLoading(false)
      }
    }

    void load()
  }, [auth.status])

  const providerOptions = catalog?.providers ?? []

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!form.label.trim() || !form.key.trim()) {
      return
    }
    setSubmitting(true)
    try {
      const created = await createKey(form)
      setKeys((current) => [created, ...current])
      setForm((current) => ({ ...current, label: '', key: '' }))
    } catch (error_) {
      setError(error_ instanceof Error ? error_.message : 'failed to create key')
    } finally {
      setSubmitting(false)
    }
  }

  const handleDelete = async (keyId: string) => {
    try {
      await deleteKey(keyId)
      setKeys((current) => current.filter((item) => item.id !== keyId))
    } catch (error_) {
      setError(error_ instanceof Error ? error_.message : 'failed to delete key')
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
        <h1 className="section-title">BYOK keys</h1>
        <div className="muted">Encrypted-at-rest provider keys for the control plane.</div>
        {error ? <div className="badge badge-error">{error}</div> : null}
      </section>

      <section className="grid-2">
        <div className="card stack">
          <h2 className="section-title">Create key</h2>
          <form className="form-grid" onSubmit={handleSubmit}>
            <label>
              Provider
              <select
                value={form.provider}
                onChange={(event) =>
                  setForm((current) => ({
                    ...current,
                    provider: event.target.value as ProviderName,
                  }))
                }
              >
                {providerOptions.map((provider) => (
                  <option key={provider.provider} value={provider.provider}>
                    {provider.provider}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Label
              <input
                type="text"
                value={form.label}
                onChange={(event) => setForm((current) => ({ ...current, label: event.target.value }))}
                placeholder="main"
              />
            </label>
            <label>
              API key
              <input
                type="password"
                value={form.key}
                onChange={(event) => setForm((current) => ({ ...current, key: event.target.value }))}
                placeholder="sk-..."
                autoComplete="off"
                spellCheck={false}
              />
            </label>
            <div className="actions">
              <button className="btn" type="submit" disabled={submitting}>
                {submitting ? 'Saving…' : 'Create key'}
              </button>
              <span className="muted small">Secrets are never rendered back to the browser.</span>
            </div>
          </form>
        </div>

        <div className="card stack">
          <h2 className="section-title">Catalog</h2>
          {catalog ? (
            <div className="stack">
              {catalog.providers.map((provider) => (
                <div key={provider.provider}>
                  <div className="actions" style={{ justifyContent: 'space-between' }}>
                    <strong>{provider.provider}</strong>
                    <span className="muted small">{provider.models.length} models</span>
                  </div>
                  <div className="muted small">
                    {provider.models.length > 0 ? provider.models.join(', ') : 'No catalog models yet.'}
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div className="muted">Loading model catalog…</div>
          )}
        </div>
      </section>

      <section className="card stack">
        <h2 className="section-title">Saved keys</h2>
        {loading ? (
          <div className="muted">Loading keys…</div>
        ) : keys.length === 0 ? (
          <div className="muted">No keys saved yet.</div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Provider</th>
                  <th>Label</th>
                  <th>Created</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {keys.map((key) => (
                  <tr key={key.id}>
                    <td>{key.provider}</td>
                    <td>{key.label}</td>
                    <td>{new Date(key.created_at).toLocaleString()}</td>
                    <td>
                      <button className="btn secondary" type="button" onClick={() => handleDelete(key.id)}>
                        Delete
                      </button>
                    </td>
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
