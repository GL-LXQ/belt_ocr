export interface Envelope<T> {
  success: boolean
  data: T
  message: string
}
export interface Machine {
  id: number
  machine_name: string
  camera_serial: string
  frequency_meter_serial: string
  enabled: boolean
  remark: string
  created_at?: string
  updated_at?: string
}
export interface RuntimeMachine {
  id: string
  machine_name: string
  camera_serial: string
  frequency_meter_serial: string
  enabled: boolean
  camera_state: string
  camera_error: string
  status: 'online' | 'offline' | 'fault'
  warning: string
  active_session_id: string | null
  waiting_cycle_reset: boolean
  inflight_count: number
}
export interface Session {
  machine_id: string
  session_id: string
  state: string
  cycle_closed: boolean
  stages: Record<string, string>
  recognized_lines: string[]
  final_frequency_hz: number | null
  start_time: string | null
  finish_time: string | null
  errors: string[]
}
export interface Snapshot {
  status: 'stopped' | 'starting' | 'running' | 'stopping' | 'failed'
  running: boolean
  failure: string
  started_at: string | null
  sequence: number
  machines: RuntimeMachine[]
  sessions: Session[]
}
export interface Measurement {
  session_id: string
  machine_id: string
  machine_name: string
  start_time?: string
  finish_time: string
  recognized_lines: string[]
  final_frequency_hz: number | null
  evidence_directory: string
  needs_review: boolean
  review_reason?: string | null
  reviewed_at: string | null
  reviewed_lines: string[] | null
}
export interface RecordPage {
  records: Measurement[]
  page: number
  page_size: number
  total: number
  total_pages: number
}
export interface RecordMachine {
  machine_id: string
  machine_name: string
}
export interface RecordFilters {
  review_status: string
  machine_id: string
  start_date: string
  end_date: string
  text_query: string
  text_match_mode: string
  text_length: string
}
export interface AbnormalEvent {
  abnormal_event_id: number
  created_at: number
  machine_id: string
  session_id: string | null
  reason: string
  payload_json: string
  payload_summary?: string
}
export interface EvidenceImage {
  image_id: string
  filename: string
  url: string
  thumbnail_url: string
}
export interface EvidencePage {
  images: EvidenceImage[]
  state: string
  count: number | null
  page: number
  page_size: number
  total_pages: number
}
export type Configuration = Record<
  string,
  string | number | boolean | null | Record<string, number | string | null>
>
export interface BackendStatus {
  processRunning?: boolean
  retryAllowed?: boolean
  phase: 'starting' | 'ready' | 'stopping' | 'stopped' | 'failed'
  message: string
}
