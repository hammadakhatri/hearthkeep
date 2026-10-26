/** Shapes of GET /api/models. */

export type ModelInfo = {
  id: string
  name: string
  kind: 'chat' | 'image'
  capabilities: string[]
  vram_gb: number
  source: 'shipped' | 'owner'
  resident: boolean
  managed: boolean
}

export type CardError = { id_or_file: string; message: string }

export type GpuStatus = {
  total_gb: number
  used_gb: number
  resident: string[]
  waiting: number
}

export type ModelsResponse = {
  models: ModelInfo[]
  errors: CardError[]
  gpu: GpuStatus
}
