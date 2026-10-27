/** The exact rows behind a finding: the file's row numbers and the columns, with a CSV download. */

import { useId } from 'react'
import { outputDownloadUrl, type RunTable } from '../api/runs'

function cell(value: unknown): string {
  if (value === null || value === undefined) return ''
  if (typeof value === 'object') {
    try {
      return JSON.stringify(value)
    } catch {
      return String(value)
    }
  }
  return String(value)
}

export function ProblemTable({ table }: { table: RunTable }) {
  const id = useId()
  // A reshaped table has no row numbers of its own; then there is no row column at all.
  const numbers = table.row_numbers.length && table.row_numbers.length === table.rows.length ? table.row_numbers : null
  const cut = table.rows.length < table.total_rows
  return (
    <section className="problem-table" aria-labelledby={`${id}-name`}>
      <div className="problem-head">
        <h4 id={`${id}-name`} className="problem-name">
          {table.name}
        </h4>
        <a className="btn problem-download" href={outputDownloadUrl(table.id)} download aria-describedby={`${id}-name`}>
          Download rows
        </a>
      </div>
      {table.note && <p className="problem-note">{table.note}</p>}
      <div className="problem-scroll" tabIndex={0} role="group" aria-label={table.name}>
        <table aria-labelledby={`${id}-name`}>
          <thead>
            <tr>
              {numbers && <th scope="col">Row</th>}
              {table.columns.map((column, i) => (
                <th key={i} scope="col">
                  {column}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {table.rows.map((row, r) => (
              <tr key={r}>
                {numbers && <td className="problem-row">{numbers[r]}</td>}
                {table.columns.map((_, c) => (
                  <td key={c}>{cell(row[c])}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {cut && (
        <p className="problem-cut">
          {`Showing ${table.rows.length} of ${table.total_rows} rows`}
        </p>
      )}
    </section>
  )
}
