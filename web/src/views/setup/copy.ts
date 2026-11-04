/** The setup wizard's own sentences, in one place so the copy rules can check every one. Copy the
 *  wizard shares with Settings (model rows, downloads, start at login, the sandbox) stays with
 *  those components. */
export const SETUP_COPY = {
  welcomeTitle: 'Welcome to Hearthkeep',
  welcomeAbout: 'Hearthkeep is a private assistant that runs on this PC. Your chats, files and pictures stay here.',
  welcomePlan:
    "Setup looks at what this PC can run, helps you choose a chat model and add-ons, downloads them, and creates the passkey you'll use to open Hearthkeep.",
  welcomeTime: 'It takes about 10 to 30 minutes, mostly downloading.',

  pcTitle: 'Your PC',
  noNvidia: 'No NVIDIA graphics card',
  notKnown: 'Not known',
  pcFailed: "Couldn't check this PC. Try again.",
  defaultFolder: 'The default folder.',
  moveHint: 'Setup moves what it has made so far to the new folder and carries on there.',
  moving: 'Moving the data folder…',
  tooLate:
    "The data folder can't move now: a download has started, the sandbox is building, or you chose the chat model or your model server.",
  moveFailed: "The data folder couldn't be moved. Try again.",

  chatTitle: 'Chat model',
  chatLede:
    'Choose the model Hearthkeep chats with. The recommended one suits this PC. Nothing downloads until the Downloads step.',
  chatExternal: 'Chat will use your model server.',
  chatLocal: 'Chat will use this model.',

  addonsTitle: 'Add-ons',
  addonsLede: 'Add what you want now, or later from Settings.',
  github: 'Add your GitHub token in Settings › GitHub after setup.',
  phone: 'You can add your phone later, from Settings.',

  downloadsTitle: 'Downloads',
  keepGoing: 'You can go on while these download. They keep going while you set up your passkey.',
  nothingChosen: 'Nothing is chosen to download. You can add models later in Settings › Chat model and Image models.',
  dataMoving: 'The data folder is moving. Try again in a moment.',

  passkeyTitle: 'Your passkey',

  doneTitle: 'Hearthkeep is ready',
  doneSetUp: "What's set up",
  doneNothing: 'Nothing else yet. Add more from Settings whenever you like.',
  doneStillDownloading: 'Still downloading',
  doneNotDownloaded: 'Not downloaded',
  doneStopped: "These didn't finish. Download them again in Settings.",
  doneExternal: 'Chat: a model server you run',
  doneAnalysis: 'Analysis',
  doneStartup: 'Start at login',
} as const

/** "The recommended chat model and its engine need about 11.0 GB of free space." */
export function diskLine(size: string, withEngine: boolean): string {
  return withEngine
    ? `The recommended chat model and its engine need about ${size} of free space.`
    : `The recommended chat model needs about ${size} of free space.`
}

export function notEnoughSpace(neededGb: number, freeGb: number): string {
  return `There isn't enough free space there: ${neededGb} GB is needed and ${freeGb} GB is free.`
}

/** Where the downloads that carry on after setup show their progress. */
export function carryOnLine(chat: boolean, images: boolean): string {
  const where =
    chat && images ? 'Settings › Chat model and Settings › Image models' : images ? 'Settings › Image models' : 'Settings › Chat model'
  return `They'll carry on in a moment, when Hearthkeep starts on its own. You'll see their progress in ${where}.`
}
