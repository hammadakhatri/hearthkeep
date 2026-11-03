import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { DiffView } from '../components/DiffView'

const TWO_FILES = [
  'diff --git a/src/app.py b/src/app.py',
  'index 111..222 100644',
  '--- a/src/app.py',
  '+++ b/src/app.py',
  '@@ -1,3 +1,3 @@',
  ' import os',
  '-print("old")',
  '+print("new")',
  ' done',
  'diff --git a/README.md b/README.md',
  '--- a/README.md',
  '+++ b/README.md',
  '@@ -2 +2 @@',
  '-a',
  '+b',
].join('\n')

describe('DiffView', () => {
  it('colours added and removed lines and labels each file', () => {
    render(<DiffView diff={TWO_FILES} />)
    const headings = screen.getAllByRole('heading')
    expect(headings.map((h) => h.textContent)).toEqual(['src/app.py', 'README.md'])
    const added = screen.getByText('print("new")')
    const removed = screen.getByText('print("old")')
    expect(added.closest('[data-kind]')).toHaveAttribute('data-kind', 'add')
    expect(removed.closest('[data-kind]')).toHaveAttribute('data-kind', 'del')
    expect(screen.getByText('import os').closest('[data-kind]')).toHaveAttribute('data-kind', 'context')
    expect(screen.getAllByText('@@ -1,3 +1,3 @@')[0].closest('[data-kind]')).toHaveAttribute('data-kind', 'hunk')
    // Line numbers for both sides: the removed line is old line 2, the added one is new line 2.
    const addedRow = added.closest('[data-kind]') as HTMLElement
    const removedRow = removed.closest('[data-kind]') as HTMLElement
    expect(addedRow.querySelector('[data-side="new"]')).toHaveTextContent('2')
    expect(addedRow.querySelector('[data-side="old"]')).toBeEmptyDOMElement()
    expect(removedRow.querySelector('[data-side="old"]')).toHaveTextContent('2')
    expect(removedRow.querySelector('[data-side="new"]')).toBeEmptyDOMElement()
  })

  it('shows the cut note for a capped diff', () => {
    render(<DiffView diff={`${TWO_FILES}\n… diff cut at 200 KB`} />)
    expect(screen.getByText('… diff cut at 200 KB')).toHaveClass('diff-note')
  })

  it('folds files when there are more than five', () => {
    const many = Array.from({ length: 6 }, (_, i) =>
      [`diff --git a/f${i}.txt b/f${i}.txt`, `--- a/f${i}.txt`, `+++ b/f${i}.txt`, '@@ -1 +1 @@', '-x', `+line${i}`].join('\n'),
    ).join('\n')
    render(<DiffView diff={many} />)
    expect(screen.getAllByRole('heading')).toHaveLength(6)
    expect(screen.queryByText('line0')).toBeNull()
    const first = screen.getByRole('button', { name: 'f0.txt' })
    expect(first).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(first)
    expect(first).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByText('line0')).toBeInTheDocument()
    expect(screen.queryByText('line1')).toBeNull()
  })

  it('alwaysOpen shows every line of every file even past five files', () => {
    const many = Array.from({ length: 6 }, (_, i) =>
      [`diff --git a/f${i}.txt b/f${i}.txt`, `--- a/f${i}.txt`, `+++ b/f${i}.txt`, '@@ -1 +1 @@', '-x', `+line${i}`].join('\n'),
    ).join('\n')
    render(<DiffView diff={many} alwaysOpen />)
    for (let i = 0; i < 6; i++) expect(screen.getByText(`line${i}`)).toBeInTheDocument()
  })

  it('keeps the whole path of a deleted file with spaces', () => {
    const diff = ['diff --git a/my file.txt b/my file.txt', 'deleted file mode 100644', '--- a/my file.txt', '+++ /dev/null', '@@ -1 +0,0 @@', '-bye'].join('\n')
    render(<DiffView diff={diff} />)
    expect(screen.getAllByRole('heading').map((h) => h.textContent)).toEqual(['my file.txt'])
  })

  it('keeps five or fewer files open', () => {
    render(<DiffView diff={TWO_FILES} />)
    expect(screen.getByRole('button', { name: 'README.md' })).toHaveAttribute('aria-expanded', 'true')
  })

  it('names a deleted file and shows large-file deletions as a note', () => {
    const diff = [
      'diff --git a/old.txt b/old.txt',
      'deleted file mode 100644',
      '--- a/old.txt',
      '+++ /dev/null',
      '@@ -1 +0,0 @@',
      '-bye',
      'Binary or large file big.bin deleted',
    ].join('\n')
    render(<DiffView diff={diff} />)
    expect(screen.getAllByRole('heading').map((h) => h.textContent)).toEqual(['old.txt'])
    expect(screen.getByText('Binary or large file big.bin deleted')).toHaveClass('diff-note')
  })

  it('treats content as text, never markup', () => {
    const diff = ['diff --git a/x b/x', '--- a/x', '+++ b/x', '@@ -0,0 +1 @@', '+<img src=x onerror=alert(1)>'].join('\n')
    const { container } = render(<DiffView diff={diff} />)
    expect(container.querySelector('img')).toBeNull()
    expect(within(container).getByText('<img src=x onerror=alert(1)>')).toBeInTheDocument()
  })

  it('does not mistake a removed line starting with dashes for a file header', () => {
    const diff = ['diff --git a/x b/x', '--- a/x', '+++ b/x', '@@ -1,2 +1,1 @@', '--- not a header', '-gone', '+kept'].join('\n')
    render(<DiffView diff={diff} />)
    expect(screen.getAllByRole('heading')).toHaveLength(1)
    expect(screen.getByText('-- not a header').closest('[data-kind]')).toHaveAttribute('data-kind', 'del')
  })
})
