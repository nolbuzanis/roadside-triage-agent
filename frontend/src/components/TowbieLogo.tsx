interface TowbieLogoProps {
  markSize?: number
}

export default function TowbieLogo({ markSize = 32 }: TowbieLogoProps) {
  return (
    <span className="brand-logo" role="img" aria-label="Towbie">
      <img
        src="/towbie-icon.svg"
        alt=""
        aria-hidden="true"
        width={markSize}
        height={markSize}
      />
      <span className="brand-wordmark" aria-hidden="true">
        Towb
        <span className="brand-i">
          &#x131;
          <span className="brand-dot-mark" />
        </span>
        e
      </span>
    </span>
  )
}
