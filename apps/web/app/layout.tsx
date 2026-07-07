import type { ReactNode } from 'react'

import { AppHeader } from '@/components/AppHeader'
import { AuthProvider } from '@/components/AuthProvider'
import './globals.css'

export const metadata = {
  title: 'Furnace',
  description: 'Live control-plane console for sessions',
}

export default function RootLayout({ children }: { children: ReactNode }): JSX.Element {
  return (
    <html lang="en">
      <body>
        <AuthProvider>
          <AppHeader />
          {children}
        </AuthProvider>
      </body>
    </html>
  )
}
