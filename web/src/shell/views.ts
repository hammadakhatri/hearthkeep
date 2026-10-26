import type { ComponentType } from 'react'
import {
  AnalysisIcon,
  ChatIcon,
  FilesIcon,
  ImagesIcon,
  ReposIcon,
  TodayIcon,
  ToolsIcon,
  type IconProps,
} from './icons'

export type ViewId = 'today' | 'chat' | 'images' | 'analysis' | 'repos' | 'files' | 'tools'

export type ViewInfo = {
  title: string
  icon: ComponentType<IconProps>
  /** One sentence on what the view is for, in the owner's words. */
  blurb: string
}

export const VIEWS: Record<ViewId, ViewInfo> = {
  today: { title: 'Today', icon: TodayIcon, blurb: 'Your morning briefing: what needs you, the news and ideas worth your time.' },
  chat: { title: 'Chat', icon: ChatIcon, blurb: 'Ask anything. Your assistant works on your own computer.' },
  images: { title: 'Images', icon: ImagesIcon, blurb: 'Create and edit images with the models on your computer.' },
  analysis: { title: 'Analysis', icon: AnalysisIcon, blurb: 'Ask questions about your data and get answers with charts.' },
  repos: { title: 'Repos', icon: ReposIcon, blurb: 'Work on your private code. Every change waits for your approval.' },
  files: { title: 'Files', icon: FilesIcon, blurb: 'Find files on your computer and send them to your devices.' },
  tools: {
    title: 'Tools & permissions',
    icon: ToolsIcon,
    blurb: 'Choose what your assistant may do on its own, and what asks you first.',
  },
}

/** Sidebar order; Tools & permissions sits apart at the bottom. */
export const MAIN_VIEWS: ViewId[] = ['today', 'chat', 'images', 'analysis', 'repos', 'files']
export const FOOTER_VIEWS: ViewId[] = ['tools']

/** Phone layout: four tabs plus More, which holds the rest. */
export const TAB_VIEWS: ViewId[] = ['today', 'chat', 'images', 'files']
export const MORE_VIEWS: ViewId[] = ['analysis', 'repos', 'tools']

export const DEFAULT_VIEW: ViewId = 'chat'
