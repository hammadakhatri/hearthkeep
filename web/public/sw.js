/*
 * Hearthkeep's service worker. Its only job is the payload-free notice: a push carries no
 * content, so this shows the same fixed words every time and never looks at what arrived.
 * Plain script, no dependencies, no caching.
 */

self.addEventListener('install', () => {
  self.skipWaiting()
})

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim())
})

self.addEventListener('push', (event) => {
  event.waitUntil(
    self.registration.showNotification('Hearthkeep', {
      body: 'A file is ready in Hearthkeep',
      tag: 'hearthkeep-sent',
      icon: '/icon-192.png',
      badge: '/icon-192.png',
    }),
  )
})

self.addEventListener('notificationclick', (event) => {
  event.notification.close()
  event.waitUntil(
    (async () => {
      const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true })
      const open = windows.find((client) => new URL(client.url).origin === self.location.origin)
      if (open) {
        // #sent first, so a window that isn't showing the app yet (signing in, say) opens
        // the tray once it does; the message opens it at once in a window that is.
        const target = new URL(open.url)
        target.hash = 'sent'
        try {
          if (typeof open.navigate === 'function') await open.navigate(target.href)
        } catch {
          // an uncontrolled window can't be navigated: the message still reaches it
        }
        open.postMessage({ type: 'open-sent' })
        await open.focus()
        return
      }
      await self.clients.openWindow('/#sent')
    })(),
  )
})
