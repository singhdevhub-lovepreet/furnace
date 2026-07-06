'use client'

import { useEffect, useState } from 'react'

import { useAuth } from '@/components/AuthProvider'
import { EventOut, getWsBase } from '@/lib/api'

export function useSessionEvents(sessionId: string): {
  events: EventOut[]
  connected: boolean
  error: string | null
} {
  const auth = useAuth()
  const [events, setEvents] = useState<EventOut[]>([])
  const [connected, setConnected] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!sessionId || auth.status !== 'authenticated' || !auth.token) {
      return
    }
    const socket = new WebSocket(
      `${getWsBase()}/ws/sessions/${sessionId}?token=${encodeURIComponent(auth.token)}`,
    )
    socket.onopen = () => {
      setConnected(true)
      setError(null)
      setEvents([])
    }
    socket.onmessage = (event) => {
      const payload = JSON.parse(event.data as string) as EventOut
      setEvents((current) => [...current, payload])
    }
    socket.onerror = () => {
      setError('event stream error')
    }
    socket.onclose = () => {
      setConnected(false)
    }
    return () => {
      socket.close()
    }
  }, [auth.status, auth.token, sessionId])

  return { events, connected, error }
}
