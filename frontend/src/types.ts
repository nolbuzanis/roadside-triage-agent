export type IntakeStatus = 'in_progress' | 'completed' | 'abandoned' | 'escalated'

export interface AssistanceRequest {
  id: string
  call_id: string
  caller_phone: string | null
  location: string | null
  vehicle: string | null
  issue: string | null
  status: string
  intake_status: IntakeStatus
  hazard_detected: boolean
  hazard_reason: string | null
  notification_status: string
  created_at: string
  updated_at: string
  demo_session_id: string | null
}

export type TranscriptSpeaker = 'You' | 'AI Agent'

export interface TranscriptTurn {
  id: string
  speaker: TranscriptSpeaker
  text: string
  seq: number
  created_at: string
}
