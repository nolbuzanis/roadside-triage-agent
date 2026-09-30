type GtagParams = Record<string, string | number | boolean | undefined>

declare global {
  interface Window {
    dataLayer?: unknown[]
    gtag?: (...args: unknown[]) => void
  }
}

const GA_ID_PATTERN = /^G-[A-Z0-9]{4,}$/

function configuredMeasurementId(): string | null {
  const raw = import.meta.env.VITE_GA_MEASUREMENT_ID?.trim()
  return raw && raw.length > 0 ? raw : null
}

export function getAnalyticsMeasurementId(): string | null {
  return configuredMeasurementId()
}

export function isAnalyticsEnabled(): boolean {
  const id = configuredMeasurementId()
  return id !== null && GA_ID_PATTERN.test(id)
}

let initialized = false

export function initAnalytics(): boolean {
  if (initialized) {
    return isAnalyticsEnabled()
  }
  initialized = true

  const measurementId = configuredMeasurementId()
  if (!measurementId) {
    console.warn(
      'Google Analytics is disabled: VITE_GA_MEASUREMENT_ID is not set.',
    )
    return false
  }
  if (!GA_ID_PATTERN.test(measurementId)) {
    console.error(
      `Google Analytics is disabled: VITE_GA_MEASUREMENT_ID has an invalid format: ${measurementId}`,
    )
    return false
  }

  window.dataLayer = window.dataLayer ?? []
  window.gtag = function gtag(...args: unknown[]) {
    window.dataLayer?.push(args)
  }
  window.gtag('js', new Date())
  // Disable the automatic page_view so route-aware pageviews below are the
  // single source of truth for `/` vs `/admin`.
  window.gtag('config', measurementId, { send_page_view: false })

  const script = document.createElement('script')
  script.async = true
  script.src = `https://www.googletagmanager.com/gtag/js?id=${encodeURIComponent(measurementId)}`
  document.head.appendChild(script)
  return true
}

export function trackPageView(pagePath: string): void {
  const measurementId = configuredMeasurementId()
  if (!measurementId || !GA_ID_PATTERN.test(measurementId)) {
    return
  }
  if (typeof window.gtag !== 'function') {
    return
  }
  window.gtag('config', measurementId, {
    page_path: pagePath,
    page_title: document.title,
  })
}

export function trackEvent(
  eventName: string,
  params: GtagParams = {},
): void {
  if (!isAnalyticsEnabled()) {
    return
  }
  if (typeof window.gtag !== 'function') {
    return
  }
  window.gtag('event', eventName, params)
}
