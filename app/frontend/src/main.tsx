import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { AuthProvider } from './auth'

// `AuthProvider` wraps `App`, and therefore wraps the router inside it, so a
// route component can ask who is signed in. It also installs the api client's
// token provider before anything below it renders — see `AccessTokenBridge`.
createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <AuthProvider>
      <App />
    </AuthProvider>
  </StrictMode>,
)
