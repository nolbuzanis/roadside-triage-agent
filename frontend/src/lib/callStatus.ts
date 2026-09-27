import type { AssistanceRequest } from '../types'

export type DemoCallPhase = 'waiting' | 'active' | 'ended'

/**
 * Derive the demo call phase from the linked request. A linked row only
 * proves a call happened — the call itself is over once intake leaves
 * `in_progress`, so terminal rows (completed/abandoned/escalated) must
 * never render as a live connection.
 */
export function demoCallPhase(
  request: AssistanceRequest | null,
): DemoCallPhase {
  if (!request) {
    return 'waiting'
  }
  return request.intake_status === 'in_progress' ? 'active' : 'ended'
}
