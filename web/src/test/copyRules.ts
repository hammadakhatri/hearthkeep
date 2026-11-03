/**
 * Owner-visible copy never names a command or shows a placeholder. The same patterns, flags and
 * hits as server/tests/copy_rules.py; a plain "hearthkeep " check would flag copy that starts with
 * the product's name. Shared so every view's copy test uses one definition.
 */
export const FORBIDDEN_COPY: RegExp[] = [
  /\bhearthkeep (serve|sandbox|secrets|startup|enroll-code|setup|launch|open)\b/gi,
  /<data>|<home>|--data\b|\buv run\b/g,
]

/** Every forbidden piece found in `text` (empty when the copy is clean). */
export function forbiddenIn(text: string): string[] {
  return FORBIDDEN_COPY.flatMap((pattern) => [...text.matchAll(pattern)].map((hit) => hit[0]))
}
