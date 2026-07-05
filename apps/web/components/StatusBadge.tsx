import { SessionStatus, statusClassName } from '@/lib/api'

export function StatusBadge({ status }: { status: SessionStatus }): JSX.Element {
  return <span className={statusClassName(status)}>{status}</span>
}
