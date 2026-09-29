// Post-call demo feedback destination.
// Set VITE_FEEDBACK_URL (e.g. in frontend/.env.local for local dev, or the
// VITE_FEEDBACK_URL GitHub repository variable for production builds) to
// point "Send feedback" at the real destination. Falls back to a same-page
// placeholder anchor when unset so the CTA still renders.
const configured = import.meta.env.VITE_FEEDBACK_URL?.trim()

export const FEEDBACK_URL =
  configured && configured.length > 0 ? configured : '#demo-feedback'
