import { useState } from 'react'

/**
 * Builds the shareable URL for a Space.
 *
 * Points at `/s/{public_id}`, the link-holder route task 2.10 owns. Naming it
 * here before that route exists is deliberate: this link *is* the capability —
 * it is the only way anyone else reaches the Space, since nothing enumerates
 * Spaces — so the admin needs to be able to copy it the moment the Space is
 * created, and the URL shape is already fixed by the plan.
 *
 * Absolute rather than a bare path, because the entire point is to paste it into
 * a chat window where a relative path means nothing.
 */
export function spaceShareUrl(publicId: string, origin: string = window.location.origin): string {
  return `${origin}/s/${publicId}`
}

/**
 * The Space's link, with a copy button.
 *
 * ## Why the URL is shown and not just copied
 *
 * Clipboard access can fail — an insecure origin, a browser that withholds
 * permission, a headless environment — and a button whose only feedback is
 * "Copied!" gives the admin no way to recover when it silently did not. The text
 * is therefore selectable on screen, and the button is a shortcut rather than
 * the sole route to the link. When the write throws, the failure is stated and
 * the link is still right there to select by hand.
 */
export function ShareLink({ publicId }: { publicId: string }) {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const url = spaceShareUrl(publicId)

  async function handleCopy() {
    try {
      await navigator.clipboard.writeText(url)
      setState('copied')
    } catch {
      setState('failed')
    }
  }

  return (
    <div className="mt-2">
      <label className="block text-xs text-slate-600" htmlFor="share-link">
        Share link
      </label>
      <div className="mt-1 flex flex-wrap items-center gap-2">
        <code
          id="share-link"
          className="min-w-0 flex-1 truncate rounded bg-slate-100 px-2 py-1 text-xs text-slate-800"
          data-testid="share-link"
        >
          {url}
        </code>
        <button
          type="button"
          className="rounded border border-slate-300 px-2 py-1 text-sm"
          data-testid="share-link-copy"
          onClick={() => void handleCopy()}
        >
          Copy
        </button>
      </div>
      {state !== 'idle' ? (
        <p
          className={`mt-1 text-xs ${state === 'copied' ? 'text-slate-600' : 'text-red-700'}`}
          data-testid="share-link-status"
          role="status"
        >
          {state === 'copied'
            ? 'Link copied. Anyone with it can ask to join.'
            : "We couldn't copy it — select the link above and copy it yourself."}
        </p>
      ) : null}
    </div>
  )
}
