import TowbieLogo from './TowbieLogo'

export default function LandingHeader() {
  return (
    <header className="landing-top">
      <TowbieLogo />
      <span className="landing-live-pill">
        <span className="pill-dot" aria-hidden="true" />
        LIVE DEMO
      </span>
    </header>
  )
}
