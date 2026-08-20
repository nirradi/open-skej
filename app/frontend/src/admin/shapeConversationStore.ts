/**
 * The browser-side pointer to one Space's reload-safe shape conversation.
 *
 * The transcript and draft remain authoritative in the product database; this
 * stores only the opaque conversation id needed to ask the server for them
 * after a reload. The id is namespaced by Space, validated before use, and is
 * cleared whenever the server says it no longer names a usable conversation.
 */

const KEY_PREFIX = 'skej.shape-conversation.'

export interface StoredShapeConversation {
  id: number
}

function keyFor(publicId: string): string {
  return `${KEY_PREFIX}${publicId}`
}

export function readStoredShapeConversation(publicId: string): StoredShapeConversation | null {
  try {
    const raw = window.localStorage.getItem(keyFor(publicId))
    if (raw === null) return null
    const value: unknown = JSON.parse(raw)
    if (
      typeof value !== 'object' ||
      value === null ||
      !('id' in value) ||
      typeof value.id !== 'number' ||
      !Number.isSafeInteger(value.id) ||
      value.id < 1
    ) {
      window.localStorage.removeItem(keyFor(publicId))
      return null
    }
    // Older pointers may retain now-unused keys. Only the safe, opaque id is
    // meaningful; the transcript carries all current conversation state.
    return { id: value.id }
  } catch {
    // Browser storage can be unavailable or contain stale hand-edited data;
    // neither is a reason to prevent an admin from opening a fresh chat.
    return null
  }
}

export function storeShapeConversation(publicId: string, id: number): void {
  try {
    window.localStorage.setItem(keyFor(publicId), JSON.stringify({ id }))
  } catch {
    // Reload recovery is a convenience. The server remains the source of truth
    // for the current page even when private-mode storage refuses a write.
  }
}

export function clearStoredShapeConversation(publicId: string): void {
  try {
    window.localStorage.removeItem(keyFor(publicId))
  } catch {
    // Same recovery-only policy as `storeShapeConversation`.
  }
}
