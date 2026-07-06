import { clearStoredAuthSession, getStoredAuthToken, type AuthUser } from '@/lib/auth'

export type ProviderName = 'openrouter' | 'anthropic' | 'openai' | 'other'

export type SessionStatus =
  | 'QUEUED'
  | 'PROVISIONING'
  | 'CLONING_REPO'
  | 'RUNNING'
  | 'RECORDING'
  | 'OPENING_PR'
  | 'SUCCEEDED'
  | 'FAILED'
  | 'CANCELLED'

export interface ModelSelection {
  provider: ProviderName
  model: string
}

export interface ModelPolicy {
  default: ModelSelection
  roles: Record<string, ModelSelection>
}

export interface CreateSessionRequest {
  repo_id: string
  prompt: string
  model_policy: ModelPolicy | Record<string, never>
}

export interface SessionOut {
  id: string
  user_id: string
  repo_id: string
  prompt: string
  status: SessionStatus
  branch: string
  pr_number: number | null
  model_policy: Record<string, unknown>
  created_at: string
  ended_at: string | null
}

export interface EventOut {
  id: string
  session_id: string
  type: string
  payload: Record<string, unknown>
  ts: string
}

export interface ArtifactOut {
  id: string
  session_id: string
  kind: string
  object_key: string
  meta: Record<string, unknown>
}

export interface UsageOut {
  id: string
  session_id: string
  mac_seconds: number
  prompt_tokens: number
  completion_tokens: number
  mac_cost_usd: string
}

export interface RepoOut {
  id: string
  installation_id: string
  full_name: string
  default_branch: string
}

export interface PoolQueueItem {
  session_id: string
  position: number
  eta_seconds: number
}

export interface PoolScaleDecisionOut {
  current_hosts: number
  desired_hosts: number
  scale_up_by: number
  total_slots: number
  free_slots: number
  active_sessions: number
  queued_sessions: number
}

export interface PoolStatusOut {
  active_sessions: number
  capacity: number
  queue_depth: number
  queued: PoolQueueItem[]
  scale_decision: PoolScaleDecisionOut
}

export interface LlmKeyCreateRequest {
  provider: ProviderName
  label: string
  key: string
}

export interface LlmKeyOut {
  id: string
  provider: ProviderName
  label: string
  created_at: string
}

export interface AuthTokenResponse {
  access_token: string
  token_type: 'bearer'
  user: AuthUser
}

export interface AuthLoginRequest {
  email: string
  password: string
}

export interface AuthSignupRequest extends AuthLoginRequest {
  plan?: string
}

export interface ModelCatalogProvider {
  provider: ProviderName
  models: string[]
}

export interface ModelCatalog {
  providers: ModelCatalogProvider[]
}

export interface SessionListRow extends SessionOut {
  repo_name: string
}

const apiBase = process.env.NEXT_PUBLIC_API_BASE ?? 'http://localhost:8000'

function normalizePath(path: string): string {
  return path.startsWith('/') ? path : `/${path}`
}

export function getApiBase(): string {
  return apiBase
}

export function getWsBase(): string {
  return apiBase.replace(/^http/i, 'ws')
}

export function joinApiUrl(path: string): string {
  return `${apiBase}${normalizePath(path)}`
}

export function sessionArtifactContentUrl(sessionId: string, artifactId: string): string {
  return joinApiUrl(`/v1/sessions/${sessionId}/artifacts/${artifactId}/content`)
}

function isBrowser(): boolean {
  return typeof window !== 'undefined'
}

function redirectToLogin(): void {
  if (!isBrowser()) {
    return
  }
  window.location.replace('/login')
}

function unauthorizedError(message: string): Error {
  return new Error(message || '401 unauthorized')
}

function handleUnauthorized(redirectOn401: boolean): never {
  clearStoredAuthSession()
  if (redirectOn401) {
    redirectToLogin()
  }
  throw unauthorizedError('401 unauthorized')
}

function buildHeaders(headers: HeadersInit | undefined, withJsonContentType: boolean): Headers {
  const requestHeaders = new Headers(headers)
  if (withJsonContentType) {
    requestHeaders.set('content-type', 'application/json')
  }
  if (isBrowser()) {
    const token = getStoredAuthToken()
    if (token) {
      requestHeaders.set('Authorization', `Bearer ${token}`)
    }
  }
  return requestHeaders
}

interface RequestOptions {
  redirectOn401?: boolean
}

async function requestJson<T>(
  path: string,
  init?: RequestInit,
  options: RequestOptions = {},
): Promise<T> {
  const response = await fetch(joinApiUrl(path), {
    ...init,
    headers: buildHeaders(init?.headers, true),
    cache: 'no-store',
  })
  if (response.status === 401) {
    if (options.redirectOn401 ?? true) {
      handleUnauthorized(true)
    }
    clearStoredAuthSession()
    const message = await response.text()
    throw unauthorizedError(message)
  }
  if (!response.ok) {
    const message = await response.text()
    throw new Error(`${response.status} ${message || `request failed with ${response.status}`}`)
  }
  return (await response.json()) as T
}

async function requestVoid(
  path: string,
  init?: RequestInit,
  options: RequestOptions = {},
): Promise<void> {
  const response = await fetch(joinApiUrl(path), {
    ...init,
    headers: buildHeaders(init?.headers, false),
    cache: 'no-store',
  })
  if (response.status === 401) {
    if (options.redirectOn401 ?? true) {
      handleUnauthorized(true)
    }
    clearStoredAuthSession()
    const message = await response.text()
    throw unauthorizedError(message)
  }
  if (!response.ok) {
    const message = await response.text()
    throw new Error(`${response.status} ${message || `request failed with ${response.status}`}`)
  }
}

export function buildDefaultModelPolicy(provider: ProviderName, model: string): ModelPolicy {
  return {
    default: { provider, model },
    roles: {},
  }
}

export async function listSessions(): Promise<SessionOut[]> {
  return requestJson<SessionOut[]>('/v1/sessions')
}

export async function listRepos(): Promise<RepoOut[]> {
  return requestJson<RepoOut[]>('/v1/repos')
}

export async function listModels(): Promise<ModelCatalog> {
  return requestJson<ModelCatalog>('/v1/models')
}

export async function getPoolStatus(): Promise<PoolStatusOut> {
  return requestJson<PoolStatusOut>('/v1/pool')
}

export async function createSession(payload: CreateSessionRequest): Promise<SessionOut> {
  return requestJson<SessionOut>('/v1/sessions', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export async function getSession(sessionId: string): Promise<SessionOut> {
  return requestJson<SessionOut>(`/v1/sessions/${sessionId}`)
}

export async function cancelSession(sessionId: string): Promise<SessionOut> {
  return requestJson<SessionOut>(`/v1/sessions/${sessionId}/cancel`, { method: 'POST' })
}

export async function listSessionArtifacts(sessionId: string): Promise<ArtifactOut[]> {
  return requestJson<ArtifactOut[]>(`/v1/sessions/${sessionId}/artifacts`)
}

export async function getSessionUsage(sessionId: string): Promise<UsageOut> {
  return requestJson<UsageOut>(`/v1/sessions/${sessionId}/usage`)
}

export async function listKeys(): Promise<LlmKeyOut[]> {
  return requestJson<LlmKeyOut[]>('/v1/keys')
}

export async function createKey(payload: LlmKeyCreateRequest): Promise<LlmKeyOut> {
  return requestJson<LlmKeyOut>('/v1/keys', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export async function deleteKey(keyId: string): Promise<void> {
  await requestVoid(`/v1/keys/${keyId}`, { method: 'DELETE' })
}

export async function signup(payload: AuthSignupRequest): Promise<AuthTokenResponse> {
  return requestJson<AuthTokenResponse>('/v1/auth/signup', {
    method: 'POST',
    body: JSON.stringify(payload),
  }, { redirectOn401: false })
}

export async function login(payload: AuthLoginRequest): Promise<AuthTokenResponse> {
  return requestJson<AuthTokenResponse>('/v1/auth/login', {
    method: 'POST',
    body: JSON.stringify(payload),
  }, { redirectOn401: false })
}

export async function getMe(options: RequestOptions = {}): Promise<AuthUser> {
  return requestJson<AuthUser>('/v1/auth/me', undefined, options)
}

export function statusClassName(status: SessionStatus): string {
  switch (status) {
    case 'SUCCEEDED':
      return 'badge badge-success'
    case 'FAILED':
    case 'CANCELLED':
      return 'badge badge-error'
    case 'RUNNING':
    case 'RECORDING':
      return 'badge badge-info'
    case 'PROVISIONING':
    case 'CLONING_REPO':
      return 'badge badge-warn'
    case 'OPENING_PR':
      return 'badge badge-brand'
    case 'QUEUED':
    default:
      return 'badge badge-neutral'
  }
}

export function canCancel(status: SessionStatus): boolean {
  return status === 'QUEUED' || status === 'PROVISIONING' || status === 'RUNNING'
}
