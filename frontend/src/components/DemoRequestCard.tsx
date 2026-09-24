import type { AssistanceRequest } from '../types'
import { fieldValue, formatTime, STATUS_LABELS } from '../lib/requestFields'

/**
 * Live request view for the public demo. Never renders the caller's phone
 * number — only the intake fields the visitor watched being collected.
 */
export default function DemoRequestCard({
  request,
}: {
  request: AssistanceRequest
}) {
  const escalated = request.intake_status === 'escalated'

  return (
    <article
      className={escalated ? 'request-card escalated' : 'request-card'}
    >
      <div className="card-top">
        <span className={`status-badge status-${request.intake_status}`}>
          {STATUS_LABELS[request.intake_status]}
        </span>
        <time dateTime={request.created_at}>{formatTime(request.created_at)}</time>
      </div>
      <dl className="card-fields">
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
