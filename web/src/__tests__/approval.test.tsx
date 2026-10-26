import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { stepUp } from '../api/webauthn'
import { ApprovalCard, type ApprovalRequest } from '../components/ApprovalCard'
import { bodyOf, mockFetch } from '../test/fetch'

vi.mock('../api/webauthn', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/webauthn')>()),
  stepUp: vi.fn(),
}))

const request: ApprovalRequest = {
  approval_id: 'a1',
  tool: 'note',
  summary: 'Save the note "Buy milk"',
  detail: { tool: 'note', arguments: { text: 'Buy milk' } },
  requires_step_up: false,
}

beforeEach(() => {
  vi.mocked(stepUp).mockReset()
})

describe('ApprovalCard', () => {
  it('shows the summary as its title and the detail as a monospace block', () => {
    mockFetch({})
    render(<ApprovalCard request={request} status="pending" />)
    expect(screen.getByRole('group', { name: 'Save the note "Buy milk"' })).toBeInTheDocument()
    const detail = screen.getByTestId('approval-detail')
    expect(detail.tagName).toBe('PRE')
    expect(detail).toHaveTextContent('"text": "Buy milk"')
    expect(screen.queryByText('Approving asks for Face ID / Touch ID')).toBeNull()
  })

  it('approval card approve without step-up posts approve:true', async () => {
    const { calls } = mockFetch({ 'POST /api/approvals/a1': { body: { id: 'a1', status: 'approved' } } })
    const onResolved = vi.fn()
    render(<ApprovalCard request={request} status="pending" onResolved={onResolved} />)
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    expect(await screen.findByText('Approved')).toBeInTheDocument()
    expect(stepUp).not.toHaveBeenCalled()
    expect(bodyOf(calls[0])).toEqual({ approve: true })
    expect(onResolved).toHaveBeenCalledWith('approved')
  })

  it('approval card with requires_step_up calls stepUp before posting and sends token', async () => {
    const order: string[] = []
    vi.mocked(stepUp).mockImplementation(async () => {
      order.push('stepUp')
      return 'token-1'
    })
    const { calls } = mockFetch({
      'POST /api/approvals/a1': () => {
        order.push('post')
        return { body: { id: 'a1', status: 'approved' } }
      },
    })
    render(<ApprovalCard request={{ ...request, requires_step_up: true }} status="pending" />)
    expect(screen.getByText('Approving asks for Face ID / Touch ID')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    expect(await screen.findByText('Approved')).toBeInTheDocument()
    expect(stepUp).toHaveBeenCalledWith('approval', 'a1')
    expect(order).toEqual(['stepUp', 'post'])
    expect(bodyOf(calls[0])).toEqual({ approve: true, step_up_token: 'token-1' })
  })

  it('declining never asks for step-up', async () => {
    const { calls } = mockFetch({ 'POST /api/approvals/a1': { body: { id: 'a1', status: 'declined' } } })
    render(<ApprovalCard request={{ ...request, requires_step_up: true }} status="pending" />)
    fireEvent.click(screen.getByRole('button', { name: 'Decline' }))
    expect(await screen.findByText('Declined')).toBeInTheDocument()
    expect(stepUp).not.toHaveBeenCalled()
    expect(bodyOf(calls[0])).toEqual({ approve: false })
  })

  it('approval card shows "Already handled" on 409', async () => {
    mockFetch({ 'POST /api/approvals/a1': { status: 409, body: { reason: 'already_resolved' } } })
    render(<ApprovalCard request={request} status="pending" />)
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    expect(await screen.findByText('Already handled')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull()
  })

  it('says when the request expired', async () => {
    mockFetch({ 'POST /api/approvals/a1': { status: 409, body: { reason: 'expired' } } })
    render(<ApprovalCard request={request} status="pending" />)
    fireEvent.click(screen.getByRole('button', { name: 'Decline' }))
    expect(await screen.findByText('This request expired')).toBeInTheDocument()
  })

  it('asks for Face ID / Touch ID again when the server wants a fresh step-up', async () => {
    vi.mocked(stepUp).mockResolvedValueOnce('stale').mockResolvedValueOnce('fresh')
    let attempt = 0
    const { calls } = mockFetch({
      'POST /api/approvals/a1': () =>
        ++attempt === 1
          ? { status: 403, body: { reason: 'step_up_required' } }
          : { body: { id: 'a1', status: 'approved' } },
    })
    render(<ApprovalCard request={{ ...request, requires_step_up: true }} status="pending" />)
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    expect(await screen.findByText('Approved')).toBeInTheDocument()
    expect(stepUp).toHaveBeenCalledTimes(2)
    expect(bodyOf(calls[1])).toEqual({ approve: true, step_up_token: 'fresh' })
  })

  it('stays open when Face ID / Touch ID is cancelled', async () => {
    vi.mocked(stepUp).mockRejectedValue(Object.assign(new Error('no'), { name: 'NotAllowedError' }))
    const { calls } = mockFetch({})
    render(<ApprovalCard request={{ ...request, requires_step_up: true }} status="pending" />)
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Approval was cancelled.')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Approve' })).toBeEnabled())
    expect(calls).toHaveLength(0)
  })
})
