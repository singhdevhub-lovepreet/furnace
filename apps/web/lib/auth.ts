export interface AuthUser {
  id: string
  email: string
  plan: string
  created_at: string
}

export interface AuthSession {
  token: string
  user: AuthUser
}

const AUTH_TOKEN_KEY = 'furnace.auth.token'
const AUTH_USER_KEY = 'furnace.auth.user'
const AUTH_CHANGE_EVENT = 'furnace-auth-change'

function hasWindow(): boolean {
  return typeof window !== 'undefined'
}

function safeParseUser(value: string | null): AuthUser | null {
  if (!value) {
    return null
  }
  try {
    const user = JSON.parse(value) as AuthUser
    if (
      typeof user.id === 'string' &&
      typeof user.email === 'string' &&
      typeof user.plan === 'string' &&
      typeof user.created_at === 'string'
    ) {
      return user
    }
  } catch {
    return null
  }
  return null
}

function notifyAuthChange(): void {
  if (!hasWindow()) {
    return
  }
  window.dispatchEvent(new Event(AUTH_CHANGE_EVENT))
}

export function getStoredAuthSession(): AuthSession | null {
  if (!hasWindow()) {
    return null
  }
  const token = window.localStorage.getItem(AUTH_TOKEN_KEY)
  const user = safeParseUser(window.localStorage.getItem(AUTH_USER_KEY))
  if (!token || !user) {
    return null
  }
  return { token, user }
}

export function getStoredAuthToken(): string | null {
  return getStoredAuthSession()?.token ?? null
}

export function setStoredAuthSession(session: AuthSession, notify = true): void {
  if (!hasWindow()) {
    return
  }
  window.localStorage.setItem(AUTH_TOKEN_KEY, session.token)
  window.localStorage.setItem(AUTH_USER_KEY, JSON.stringify(session.user))
  if (notify) {
    notifyAuthChange()
  }
}

export function clearStoredAuthSession(): void {
  if (!hasWindow()) {
    return
  }
  window.localStorage.removeItem(AUTH_TOKEN_KEY)
  window.localStorage.removeItem(AUTH_USER_KEY)
  notifyAuthChange()
}

export function onAuthSessionChange(callback: () => void): () => void {
  if (!hasWindow()) {
    return () => undefined
  }
  window.addEventListener(AUTH_CHANGE_EVENT, callback)
  window.addEventListener('storage', callback)
  return () => {
    window.removeEventListener(AUTH_CHANGE_EVENT, callback)
    window.removeEventListener('storage', callback)
  }
}
