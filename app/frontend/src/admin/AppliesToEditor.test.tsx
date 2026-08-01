// @vitest-environment jsdom
/**
 * Tests for `AppliesToEditor` and the pure conversions around it: always /
 * these weekdays / these dates, the whole of `applies_to`.
 */

import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  AppliesToEditor,
  appliesToDraftIsValid,
  appliesToFromDraft,
  draftFromAppliesTo,
  type AppliesToDraft,
} from './AppliesToEditor'

afterEach(() => {
  cleanup()
})

describe('draftFromAppliesTo', () => {
  it('reads null as "always"', () => {
    expect(draftFromAppliesTo(null)).toEqual({ mode: 'always' })
  })

  it('reads a weekdays object', () => {
    expect(draftFromAppliesTo({ weekdays: [1, 3] })).toEqual({
      mode: 'weekdays',
      weekdays: [1, 3],
    })
  })

  it('reads a dates object', () => {
    expect(draftFromAppliesTo({ dates: ['2026-08-01'] })).toEqual({
      mode: 'dates',
      dates: ['2026-08-01'],
    })
  })
})

describe('appliesToFromDraft', () => {
  it('converts "always" to null', () => {
    expect(appliesToFromDraft({ mode: 'always' })).toBeNull()
  })

  it('sorts and converts a weekday selection', () => {
    expect(appliesToFromDraft({ mode: 'weekdays', weekdays: [4, 1] })).toEqual({
      weekdays: [1, 4],
    })
  })

  it('collapses an empty weekday selection to null rather than an empty list', () => {
    expect(appliesToFromDraft({ mode: 'weekdays', weekdays: [] })).toBeNull()
  })

  it('filters blank date entries and converts the rest', () => {
    expect(appliesToFromDraft({ mode: 'dates', dates: ['2026-08-01', ''] })).toEqual({
      dates: ['2026-08-01'],
    })
  })

  it('collapses an all-blank date list to null', () => {
    expect(appliesToFromDraft({ mode: 'dates', dates: [''] })).toBeNull()
  })
})

describe('appliesToDraftIsValid', () => {
  it('is always valid for "always"', () => {
    expect(appliesToDraftIsValid({ mode: 'always' })).toBe(true)
  })

  it('requires at least one weekday selected', () => {
    expect(appliesToDraftIsValid({ mode: 'weekdays', weekdays: [] })).toBe(false)
    expect(appliesToDraftIsValid({ mode: 'weekdays', weekdays: [2] })).toBe(true)
  })

  it('requires at least one non-blank date', () => {
    expect(appliesToDraftIsValid({ mode: 'dates', dates: [''] })).toBe(false)
    expect(appliesToDraftIsValid({ mode: 'dates', dates: ['2026-08-01'] })).toBe(true)
  })
})

describe('AppliesToEditor', () => {
  function renderEditor(draft: AppliesToDraft, onChange = vi.fn()) {
    render(<AppliesToEditor idPrefix="r" draft={draft} onChange={onChange} />)
    return { onChange }
  }

  it('switches to weekdays mode with an empty selection', () => {
    const { onChange } = renderEditor({ mode: 'always' })

    fireEvent.click(screen.getByTestId('r-applies-mode-weekdays'))

    expect(onChange).toHaveBeenCalledWith({ mode: 'weekdays', weekdays: [] })
  })

  it('switches to dates mode with one blank date', () => {
    const { onChange } = renderEditor({ mode: 'always' })

    fireEvent.click(screen.getByTestId('r-applies-mode-dates'))

    expect(onChange).toHaveBeenCalledWith({ mode: 'dates', dates: [''] })
  })

  it('adds a checked weekday to the selection', () => {
    const { onChange } = renderEditor({ mode: 'weekdays', weekdays: [1] })

    fireEvent.click(screen.getByTestId('r-applies-weekday-2'))

    expect(onChange).toHaveBeenCalledWith({ mode: 'weekdays', weekdays: [1, 2] })
  })

  it('removes an unchecked weekday from the selection', () => {
    const { onChange } = renderEditor({ mode: 'weekdays', weekdays: [1, 2] })

    fireEvent.click(screen.getByTestId('r-applies-weekday-1'))

    expect(onChange).toHaveBeenCalledWith({ mode: 'weekdays', weekdays: [2] })
  })

  it('edits a date at its own index', () => {
    const { onChange } = renderEditor({ mode: 'dates', dates: ['', '2026-08-02'] })

    fireEvent.change(screen.getByTestId('r-applies-date-0'), {
      target: { value: '2026-08-01' },
    })

    expect(onChange).toHaveBeenCalledWith({
      mode: 'dates',
      dates: ['2026-08-01', '2026-08-02'],
    })
  })

  it('adds another blank date row', () => {
    const { onChange } = renderEditor({ mode: 'dates', dates: ['2026-08-01'] })

    fireEvent.click(screen.getByTestId('r-applies-date-add'))

    expect(onChange).toHaveBeenCalledWith({ mode: 'dates', dates: ['2026-08-01', ''] })
  })

  it('removing the last date row leaves one blank row rather than none', () => {
    const { onChange } = renderEditor({ mode: 'dates', dates: ['2026-08-01'] })

    fireEvent.click(screen.getByTestId('r-applies-date-remove-0'))

    expect(onChange).toHaveBeenCalledWith({ mode: 'dates', dates: [''] })
  })

  it('disables every control when disabled', () => {
    render(
      <AppliesToEditor
        idPrefix="r"
        draft={{ mode: 'weekdays', weekdays: [1] }}
        onChange={vi.fn()}
        disabled
      />,
    )

    expect(screen.getByTestId('r-applies-mode-always').hasAttribute('disabled')).toBe(true)
    expect(screen.getByTestId('r-applies-weekday-1').hasAttribute('disabled')).toBe(true)
  })
})
