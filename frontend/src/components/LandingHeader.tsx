import TowbieLogo from './TowbieLogo'

export default function LandingHeader() {
  return (
    <header className="landing-top">
      <TowbieLogo markSize={64} />
      <span className="landing-live-pill">
        <span className="pill-dot" aria-hidden="true" />
        LIVE DEMO
      </span>
    </header>
  )
}
