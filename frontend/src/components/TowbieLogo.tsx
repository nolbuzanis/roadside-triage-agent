interface TowbieLogoProps {
  markSize?: number
}

export default function TowbieLogo({ markSize = 32 }: TowbieLogoProps) {
  return (
    <span className="brand-logo" role="img" aria-label="Towbie">
      <img
        src="/towbie-logo.png"
        alt=""
        aria-hidden="true"
        height={markSize}
        style={{ width: 'auto' }}
      />
    </span>
  )
}
