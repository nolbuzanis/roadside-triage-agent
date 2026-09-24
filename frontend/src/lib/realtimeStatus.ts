export type RealtimeStatus = 'connecting' | 'live' | 'disconnected'

export const STATUS_INDICATOR: Record<
  RealtimeStatus,
  { label: string; className: string }
> = {
  connecting: { label: 'Connecting…', className: 'live-indicator connecting' },
  live: { label: 'Live', className: 'live-indicator live' },
  disconnected: {
    label: 'Disconnected',
    className: 'live-indicator disconnected',
  },
}
