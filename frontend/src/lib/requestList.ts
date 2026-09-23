import type { AssistanceRequest } from '../types'

function byCreatedAtDesc(a: AssistanceRequest, b: AssistanceRequest): number {
  if (a.created_at === b.created_at) {
    return 0
  }
  return a.created_at > b.created_at ? -1 : 1
}

function preferNewer(
  existing: AssistanceRequest,
  incoming: AssistanceRequest,
): AssistanceRequest {
  return incoming.updated_at >= existing.updated_at ? incoming : existing
}

/**
 * Merge one realtime INSERT/UPDATE payload into the list, keyed by `id`.
 * Never duplicates a row; stale updates (older `updated_at`) are ignored;
 * the list stays sorted newest-first by `created_at`.
 */
export function upsertRequest(
  requests: AssistanceRequest[],
  incoming: AssistanceRequest,
): AssistanceRequest[] {
  const index = requests.findIndex((request) => request.id === incoming.id)
  if (index === -1) {
    return [...requests, incoming].sort(byCreatedAtDesc)
  }
  const existing = requests[index]
  const resolved = preferNewer(existing, incoming)
  if (resolved === existing) {
    return requests
  }
  const next = requests.slice()
  next[index] = resolved
  return next.sort(byCreatedAtDesc)
}

/**
 * Merge the initial DB query result with any realtime rows that arrived
 * while the query was in flight, so neither side wins a race: loaded rows
 * are the snapshot, but a newer realtime version of the same row is kept,
 * and rows absent from the snapshot stay listed (no lost/duplicate cards).
 */
export function mergeLoadedRequests(
  current: AssistanceRequest[],
  loaded: AssistanceRequest[],
): AssistanceRequest[] {
  if (current.length === 0) {
    return loaded
  }
  const merged = new Map<string, AssistanceRequest>()
  for (const request of loaded) {
    merged.set(request.id, request)
  }
  for (const request of current) {
    const existing = merged.get(request.id)
    merged.set(request.id, existing ? preferNewer(existing, request) : request)
  }
  return [...merged.values()].sort(byCreatedAtDesc)
}
