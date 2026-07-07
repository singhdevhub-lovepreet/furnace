'use client'

import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { usePathname, useRouter } from 'next/navigation'

import {
  clearStoredAuthSession,
  getStoredAuthSession,
  onAuthSessionChange,
  setStoredAuthSession,
  type AuthSession,
  type AuthUser,
} from '@/lib/auth'
import { getMe } from '@/lib/api'

type AuthStatus = 'loading' | 'authenticated' | 'unauthenticated'

interface AuthContextValue {
  status: AuthStatus
  user: AuthUser | null
  token: string | null
  logout: () => void
  refresh: () => Promise<void>
  setSession: (session: AuthSession) => void
}

const AuthContext = createContext<AuthContextValue | null>(null)

function isProtectedPath(pathname: string | null): boolean {
  return pathname !== '/login' && pathname !== '/signup'
}

export function AuthProvider({ children }: { children: ReactNode }): JSX.Element {
  const router = useRouter()
  const pathname = usePathname()
  const [status, setStatus] = useState<AuthStatus>('loading')
  const [user, setUser] = useState<AuthUser | null>(null)
  const [token, setToken] = useState<string | null>(null)

  const hydrate = useCallback(async () => {
    const session = getStoredAuthSession()
    if (!session) {
      setStatus('unauthenticated')
      setUser(null)
      setToken(null)
      return
    }
    setStatus('loading')
    setUser(session.user)
    setToken(session.token)
    try {
      const currentUser = await getMe({ redirectOn401: false })
      const refreshedSession = { token: session.token, user: currentUser }
      setStoredAuthSession(refreshedSession, false)
      setUser(currentUser)
      setToken(session.token)
      setStatus('authenticated')
    } catch {
      clearStoredAuthSession()
      setUser(null)
      setToken(null)
      setStatus('unauthenticated')
      if (isProtectedPath(pathname)) {
        router.replace('/login')
      }
    }
  }, [pathname, router])

  useEffect(() => {
    void hydrate()
  }, [hydrate])

  useEffect(() => onAuthSessionChange(() => void hydrate()), [hydrate])

  useEffect(() => {
    if (status === 'unauthenticated' && isProtectedPath(pathname)) {
      router.replace('/login')
    }
  }, [pathname, router, status])

  const logout = useCallback(() => {
    clearStoredAuthSession()
    setStatus('unauthenticated')
    setUser(null)
    setToken(null)
    router.replace('/login')
  }, [router])

  const setSession = useCallback((session: AuthSession) => {
    setStoredAuthSession(session, false)
    setStatus('authenticated')
    setUser(session.user)
    setToken(session.token)
  }, [])

  const value = useMemo<AuthContextValue>(
    () => ({
      status,
      user,
      token,
      logout,
      refresh: hydrate,
      setSession,
    }),
    [hydrate, logout, setSession, status, token, user],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext)
  if (value === null) {
    throw new Error('useAuth must be used within AuthProvider')
  }
  return value
}
