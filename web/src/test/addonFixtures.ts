import type { Reply } from './fetch'

// The server's copy (addons.py ABOUT and _COPY), kept small: id -> [name, about, requires].
const ADDON_ABOUT: Record<string, [string, string, string]> = {
  chat: [
    'Chat model',
    'Chat with a model that runs on this PC.',
    'llama.cpp and a chat model, or a chat server you already run on this PC.',
  ],
  analysis: [
    'Analysis',
    'Python analysis of your files, and reading PDFs and scans.',
    'Docker Desktop, and the analysis sandbox (built once, about 1 GB).',
  ],
  images: ['Images', 'Create and edit images on this PC.', 'ComfyUI and at least one image model.'],
  github: [
    'GitHub',
    'Work on your GitHub repos: read, edit, test and open pull requests.',
    'Git 2.31 or newer and a GitHub token. Running tests also needs Analysis.',
  ],
  devices: [
    'Phone and devices',
    'Use Hearthkeep from your phone and other devices.',
    'Tailscale, signed in, with HTTPS turned on.',
  ],
  startup: ['Start at login', 'Hearthkeep starts on its own when you sign in.', 'Windows.'],
}

// "id.state" -> [message, action, short]
const ADDON_COPY: Record<string, [string, string | null, string | null]> = {
  'chat.not_set_up': ['Set up your chat model to start chatting.', 'open_setup', 'Needs a chat model'],
  'chat.ready': ['Ready.', null, null],
  'chat.starting': ['Loading the chat model…', null, null],
  'chat.offline': [
    "The chat server isn't answering. Make sure it's running on this PC.",
    'open_setup',
    'Chat server offline',
  ],
  'chat.error': ["Hearthkeep can't find the chat model's files on this PC.", 'open_setup', 'Chat model files missing'],
  'analysis.not_set_up': ["Docker Desktop isn't installed on this PC.", 'install', 'Needs Docker'],
  'analysis.docker_stopped': ["Docker Desktop isn't running. Start it, then check again.", 'start_docker', 'Needs Docker'],
  'analysis.needs_image': ["The analysis sandbox isn't built yet.", 'build_image', 'Needs the analysis sandbox'],
  'analysis.ready': ['Ready.', null, null],
  'images.not_set_up': ["ComfyUI and an image model aren't set up on this PC.", 'install', 'Needs Images'],
  'images.error': [
    "Hearthkeep can't find ComfyUI or the image model files on this PC.",
    'open_setup',
    'Needs Images',
  ],
  'images.ready': ['Ready.', null, null],
  'github.not_set_up': ["Git isn't installed and no GitHub token is saved.", 'install', 'Needs Git and a token'],
  'github.needs_git': ["Git 2.31 or newer isn't installed on this PC.", 'install', 'Needs Git'],
  'github.needs_token': [
    'Save a GitHub token to let Hearthkeep work on your repos.',
    'add_token',
    'Needs a GitHub token',
  ],
  'github.ready': ['Ready.', null, null],
  'devices.this_pc_only': ['Hearthkeep only answers on this PC.', 'open_setup', null],
  'devices.needs_tailscale': [
    "Remote access is on, but Tailscale isn't running or signed in on this PC.",
    'open_setup',
    null,
  ],
  'devices.ready': ['Your phone and other devices can reach Hearthkeep.', null, null],
  'startup.off': ["Hearthkeep doesn't start on its own when you sign in.", 'turn_on', null],
  'startup.on': ['Hearthkeep starts when you sign in to Windows.', null, null],
  'startup.unsupported': ['Start at login is built in only on Windows.', null, null],
}

const READY_STATE: Record<string, string> = {
  chat: 'ready',
  analysis: 'ready',
  images: 'ready',
  github: 'ready',
  devices: 'ready',
  startup: 'on',
}
const ORDER = ['chat', 'analysis', 'images', 'github', 'devices', 'startup']

/** The six add-ons as the server sends them; `states` overrides single add-ons' states. */
export function addonList(states: Partial<Record<string, string>> = {}) {
  return ORDER.map((id) => {
    const state = states[id] ?? READY_STATE[id]
    const [name, about, requires] = ADDON_ABOUT[id]
    const [message, action, short] = ADDON_COPY[`${id}.${state}`]
    return {
      id,
      name,
      about,
      requires,
      state,
      ready: state === 'ready' || state === 'on',
      message,
      action,
      short,
      details: id === 'chat' && state === 'ready' ? { model: 'Local model 12B', loaded: true } : {},
    }
  })
}

/** A `GET /api/addons` reply; `states` overrides single add-ons' states. */
export function addonsWith(states: Partial<Record<string, string>> = {}): Reply {
  return { body: { addons: addonList(states), checked_at: '2026-11-03T09:00:00+00:00' } }
}

/** All six add-ons ready. */
export const addonsReady: Reply = addonsWith()
