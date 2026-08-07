// @vitest-environment jsdom
/**
 * Tests for `RuleParamsForm` and the pure helpers around it: the generic form
 * that renders from a rule type's declared parameter schema alone.
 *
 * `SpaceRulesPage.test.tsx` is where the "this never branches on rule type"
 * property is proven end to end, against a rule type invented for that test.
 * These tests exercise the same genericity one level down: two `kind`s,
 * `"integer"` and `"local_time"`, and nothing here reads `param.name` or any
 * rule type id to decide what to render.
 */

import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { makeRuleParam } from './fixtures'
import {
  RuleParamsForm,
  emptyRuleParamValues,
  ruleParamValuesFromStored,
  ruleParamValuesToWire,
  ruleParamsAreValid,
} from './RuleParamsForm'

afterEach(() => {
  cleanup()
})

describe('emptyRuleParamValues', () => {
  it('seeds one blank string per declared parameter', () => {
    const params = [
      makeRuleParam({ name: 'a' }),
      makeRuleParam({ name: 'b', kind: 'local_time' }),
    ]

    expect(emptyRuleParamValues(params)).toEqual({ a: '', b: '' })
  })
})

describe('ruleParamValuesFromStored', () => {
  it('reads an integer as a string', () => {
    const params = [makeRuleParam({ name: 'max_duration_minutes', kind: 'integer' })]

    expect(
      ruleParamValuesFromStored(params, { max_duration_minutes: 90 }),
    ).toEqual({ max_duration_minutes: '90' })
  })

  it('renders a stored local_time (minutes from local midnight) as HH:MM for the input', () => {
    const params = [makeRuleParam({ name: 'opens_at_minutes', kind: 'local_time' })]

    expect(ruleParamValuesFromStored(params, { opens_at_minutes: 7 * 60 })).toEqual({
      opens_at_minutes: '07:00',
    })
  })

  it('wraps a local_time past 1440 minutes to an early wall-clock time', () => {
    // A window crossing local midnight (`closes_at_minutes > 1440`) is representable in the
    // engine, but a plain `<input type="time">` cannot express it — this is that known limit,
    // not a bug (`ops/done/stream-7/passed-midnight.md`).
    const params = [makeRuleParam({ name: 'closes_at_minutes', kind: 'local_time' })]

    expect(ruleParamValuesFromStored(params, { closes_at_minutes: 26 * 60 })).toEqual({
      closes_at_minutes: '02:00',
    })
  })

  it('collapses a missing, null, or wrongly-typed value to blank rather than throwing', () => {
    const params = [
      makeRuleParam({ name: 'missing', kind: 'integer' }),
      makeRuleParam({ name: 'nulled', kind: 'integer' }),
      makeRuleParam({ name: 'wrong_type', kind: 'local_time' }),
    ]

    // A broken row (ruleValidation.ts) still has to render something editable
    // rather than crash the page — a blank field is what an admin types a fix
    // into. `local_time` is stored as minutes from local midnight (a plain
    // number), so the wrong-type case for it is a non-numeric value, not a
    // number — 42 would be a legitimate (if unusual) stored value now.
    expect(
      ruleParamValuesFromStored(params, { nulled: null, wrong_type: '09:00:00' }),
    ).toEqual({ missing: '', nulled: '', wrong_type: '' })
  })
})

describe('ruleParamValuesToWire', () => {
  it('converts an integer field to a number', () => {
    const params = [makeRuleParam({ name: 'n', kind: 'integer' })]

    expect(ruleParamValuesToWire(params, { n: '45' })).toEqual({ n: 45 })
  })

  it('converts a local_time field to minutes from local midnight', () => {
    const params = [makeRuleParam({ name: 't', kind: 'local_time' })]

    expect(ruleParamValuesToWire(params, { t: '07:30' })).toEqual({ t: 7 * 60 + 30 })
  })

  it('omits a blank value entirely rather than sending null', () => {
    const params = [makeRuleParam({ name: 'n', kind: 'integer', required: false })]

    expect(ruleParamValuesToWire(params, { n: '' })).toEqual({})
  })
})

describe('ruleParamsAreValid', () => {
  it('refuses a blank required value', () => {
    const params = [makeRuleParam({ name: 'n', required: true })]

    expect(ruleParamsAreValid(params, { n: '' })).toBe(false)
  })

  it('accepts a blank optional value', () => {
    const params = [makeRuleParam({ name: 'n', required: false })]

    expect(ruleParamsAreValid(params, { n: '' })).toBe(true)
  })

  it('refuses a non-numeric integer value', () => {
    const params = [makeRuleParam({ name: 'n', kind: 'integer' })]

    expect(ruleParamsAreValid(params, { n: 'not-a-number' })).toBe(false)
  })

  it('refuses an integer value below its declared minimum', () => {
    const params = [makeRuleParam({ name: 'n', kind: 'integer', minimum: 5 })]

    expect(ruleParamsAreValid(params, { n: '4' })).toBe(false)
    expect(ruleParamsAreValid(params, { n: '5' })).toBe(true)
  })

  it('does not apply the minimum check to a local_time value', () => {
    const params = [makeRuleParam({ name: 't', kind: 'local_time', minimum: 5 })]

    expect(ruleParamsAreValid(params, { t: '07:00' })).toBe(true)
  })
})

describe('RuleParamsForm', () => {
  it('renders a number input for an integer parameter', () => {
    const params = [makeRuleParam({ name: 'n', kind: 'integer', label: 'Count' })]

    render(
      <RuleParamsForm idPrefix="p" params={params} values={{ n: '3' }} onChange={vi.fn()} />,
    )

    const input = screen.getByTestId('p-param-n') as HTMLInputElement
    expect(input.type).toBe('number')
    expect(input.value).toBe('3')
  })

  it('renders a time input for a local_time parameter', () => {
    const params = [makeRuleParam({ name: 't', kind: 'local_time', label: 'Opens' })]

    render(
      <RuleParamsForm idPrefix="p" params={params} values={{ t: '07:00' }} onChange={vi.fn()} />,
    )

    const input = screen.getByTestId('p-param-t') as HTMLInputElement
    expect(input.type).toBe('time')
    expect(input.value).toBe('07:00')
  })

  it('calls onChange with the parameter name and new value', () => {
    const params = [makeRuleParam({ name: 'n', kind: 'integer' })]
    const onChange = vi.fn()

    render(<RuleParamsForm idPrefix="p" params={params} values={{ n: '' }} onChange={onChange} />)

    fireEvent.change(screen.getByTestId('p-param-n'), { target: { value: '10' } })

    expect(onChange).toHaveBeenCalledWith('n', '10')
  })

  it('renders a "no params" notice for a type that takes none', () => {
    render(<RuleParamsForm idPrefix="p" params={[]} values={{}} onChange={vi.fn()} />)

    expect(screen.getByTestId('p-no-params')).toBeTruthy()
  })

  it('disables every field when disabled', () => {
    const params = [makeRuleParam({ name: 'n' })]

    render(
      <RuleParamsForm
        idPrefix="p"
        params={params}
        values={{ n: '1' }}
        onChange={vi.fn()}
        disabled
      />,
    )

    expect(screen.getByTestId('p-param-n').hasAttribute('disabled')).toBe(true)
  })
})
