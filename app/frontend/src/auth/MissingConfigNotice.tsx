/**
 * Shown instead of the app when the Auth0 environment is not configured.
 *
 * The failure this replaces is the reason it is worth a component. Handing the
 * SDK an empty `domain` does not raise anything legible — it builds a redirect
 * to `https:///authorize`, the browser gives up, and the developer is left with
 * a blank white page and a console message about a navigation. Nothing in that
 * chain mentions an environment variable.
 *
 * So this names the exact variables that are missing and where to put them. It
 * renders for developers, never for users: a production build without these set
 * would be caught long before anyone saw this screen.
 */
export function MissingConfigNotice({ missing }: { missing: string[] }) {
  return (
    <main
      className="min-h-screen bg-slate-50 p-8 text-slate-800"
      data-testid="auth-config-missing"
      role="alert"
    >
      <div className="mx-auto max-w-xl rounded-lg border border-amber-300 bg-amber-50 p-6">
        <h1 className="text-lg font-semibold text-amber-900">Auth0 is not configured</h1>
        <p className="mt-2 text-sm text-amber-900">
          Open-Skej cannot sign anyone in until these variables are set:
        </p>
        <ul className="mt-3 list-disc pl-5 font-mono text-sm text-amber-900">
          {missing.map((name) => (
            <li key={name}>{name}</li>
          ))}
        </ul>
        <p className="mt-4 text-sm text-amber-900">
          Copy <code className="font-mono">app/frontend/.env.example</code> to{' '}
          <code className="font-mono">app/frontend/.env.local</code> and fill them in. Running{' '}
          <code className="font-mono">python scripts/auth0_provision.py</code> in{' '}
          <code className="font-mono">app/backend</code> prints the values ready to paste, and
          restarting the Vite dev server is required — these are read at build time.
        </p>
      </div>
    </main>
  )
}
