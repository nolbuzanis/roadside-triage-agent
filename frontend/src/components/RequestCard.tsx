import type { AssistanceRequest, IntakeStatus } from '../types'

const STATUS_LABELS: Record<IntakeStatus, string> = {
  in_progress: 'In progress',
  completed: 'Completed',
  abandoned: 'Abandoned',
  escalated: 'Escalated',
}

function fieldValue(
  value: string | null | undefined,
  intakeStatus: IntakeStatus,
): string {
  if (typeof value === 'string' && value.trim() !== '') {
    return value
  }
  return intakeStatus === 'in_progress' ? 'Collecting…' : 'Not collected'
}

function formatTime(createdAt: string): string {
  return new Date(createdAt).toLocaleString()
}

export default function RequestCard({ request }: { request: AssistanceRequest }) {
  const escalated = request.intake_status === 'escalated'
  const label = STATUS_LABELS[request.intake_status]

  return (
    <article
      className={escalated ? 'request-card escalated' : 'request-card'}
    >
      <div className="card-top">
        <span className={`status-badge status-${request.intake_status}`}>
          {label}
        </span>
        <time dateTime={request.created_at}>{formatTime(request.created_at)}</time>
      </div>
      <dl className="card-fields">
        <div>
          <dt>Caller</dt>
          <dd>{fieldValue(request.caller_phone, request.intake_status)}</dd>
        </div>
        <div>
          <dt>Location</dt>
          <dd>{fieldValue(request.location, request.intake_status)}</dd>
        </div>
        <div>
          <dt>Vehicle</dt>
          <dd>{fieldValue(request.vehicle, request.intake_status)}</dd>
        </div>
        <div>
          <dt>Issue</dt>
          <dd>{fieldValue(request.issue, request.intake_status)}</dd>
        </div>
      </dl>
    </article>
  )
}
