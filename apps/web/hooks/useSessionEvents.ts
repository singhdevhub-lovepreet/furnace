'use client'

import { useEffect, useState } from 'react'

import { EventOut, getWsBase } from '@/lib/api'

export function useSessionEvents(sessionId: string): {
  events: EventOut[]
  connected: boolean
  error: string | null
} {
  const [events, setEvents] = useState<EventOut[]>([])
  const [connected, setConnected] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!sessionId) {
      return
    }
    const socket = new WebSocket(`${getWsBase()}/ws/sessions/${sessionId}`)
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
  }, [sessionId])

  return { events, connected, error }
}
