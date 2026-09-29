import { FEEDBACK_URL } from '../lib/feedback'

export default function DemoFeedbackCta() {
  return (
    <section className="demo-feedback" aria-label="Post-call feedback">
      <h2>Thanks for trying it 👋</h2>
      <p>What felt smooth? What felt weird?</p>
      <a href={FEEDBACK_URL} target="_blank" rel="noreferrer">
        Send feedback
      </a>
    </section>
  )
}
