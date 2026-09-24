import { useState, type FormEvent } from 'react'

interface DemoStartFormProps {
  onSubmit: (phone: string) => Promise<void>
  submitting: boolean
  error: string | null
}

export default function DemoStartForm({
  onSubmit,
  submitting,
  error,
}: DemoStartFormProps) {
  const [phone, setPhone] = useState('')

  async function handleSubmit(event: FormEvent) {
    event.preventDefault()
    await onSubmit(phone)
  }

  return (
    <main className="auth-screen">
      <form className="auth-card" onSubmit={handleSubmit}>
        <h1>Try the Roadside AI Demo</h1>
        <p className="auth-subtitle">Enter the phone number you&apos;ll call from</p>
        <label>
          Phone number
          <input
            type="tel"
            autoComplete="tel"
            inputMode="tel"
            placeholder="+1 604 555 1234"
            required
            value={phone}
            onChange={(event) => setPhone(event.target.value)}
          />
        </label>
        {error && (
          <p className="auth-error" role="alert">
            {error}
          </p>
        )}
        <button type="submit" disabled={submitting}>
          {submitting ? 'Starting…' : 'Start Demo'}
        </button>
      </form>
    </main>
  )
}
