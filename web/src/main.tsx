import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './styles/tokens.css'
import './styles/base.css'
import './styles/shell.css'
import './styles/signin.css'
import './styles/chat.css'
import './styles/tools.css'
import './styles/images.css'
import './styles/findings.css'
import './styles/sent.css'
import App from './App'

// The service worker shows payload-free notifications; it caches nothing.
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(() => {})
  })
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
