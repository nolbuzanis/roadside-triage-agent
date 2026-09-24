import type { IntakeStatus } from '../types'

export const STATUS_LABELS: Record<IntakeStatus, string> = {
  in_progress: 'In progress',
  completed: 'Completed',
  abandoned: 'Abandoned',
  escalated: 'Escalated',
}

export function fieldValue(
  value: string | null | undefined,
  intakeStatus: IntakeStatus,
): string {
  if (typeof value === 'string' && value.trim() !== '') {
    return value
  }
  return intakeStatus === 'in_progress' ? 'Collecting…' : 'Not collected'
}

export function formatTime(createdAt: string): string {
  return new Date(createdAt).toLocaleString()
}
