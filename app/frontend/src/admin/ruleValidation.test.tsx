/**
 * Tests for `ruleValidation.ts`: the client-side re-run of a stored row's
 * schema check, which is the only place in the product an admin can see why
 * a rule is silently refusing every booking against its Space.
 */

import { describe, expect, it } from 'vitest'

import { makeRuleParam, makeRuleType } from './fixtures'
import { findRuleTypeFor, ruleBrokenReason } from './ruleValidation'

describe('findRuleTypeFor', () => {
  it('finds a registered type by its rule_type id', () => {
    const types = [makeRuleType({ rule_type: 'max_duration' })]

    expect(findRuleTypeFor(types, 'max_duration')?.rule_type).toBe('max_duration')
  })

  it('returns undefined for a rule_type the registry does not have', () => {
    const types = [makeRuleType({ rule_type: 'max_duration' })]

    expect(findRuleTypeFor(types, 'not_a_real_type')).toBeUndefined()
  })
})

describe('ruleBrokenReason', () => {
  it('is null for a row whose params satisfy its type', () => {
    const types = [
      makeRuleType({
        rule_type: 'max_duration',
        params: [makeRuleParam({ name: 'max_duration_minutes', required: true, minimum: 1 })],
      }),
    ]
    const row = { rule_type: 'max_duration', params: { max_duration_minutes: 90 } }

    expect(ruleBrokenReason(types, row)).toBeNull()
  })

  it('names an unregistered rule_type', () => {
    const row = { rule_type: 'renamed_or_removed', params: {} }

    expect(ruleBrokenReason([], row)).toBe('"renamed_or_removed" is not a registered rule type.')
  })

  it('flags a missing required parameter', () => {
    const types = [
      makeRuleType({
        rule_type: 'max_duration',
        params: [
          makeRuleParam({ name: 'max_duration_minutes', label: 'Max duration', required: true }),
        ],
      }),
    ]
    const row = { rule_type: 'max_duration', params: {} }

    expect(ruleBrokenReason(types, row)).toBe('Missing required parameter "Max duration".')
  })

  it('does not flag a missing optional parameter', () => {
    const types = [
      makeRuleType({
        rule_type: 'max_duration',
        params: [makeRuleParam({ name: 'n', required: false })],
      }),
    ]
    const row = { rule_type: 'max_duration', params: {} }

    expect(ruleBrokenReason(types, row)).toBeNull()
  })

  it('flags an integer value stored as the wrong JS type', () => {
    const types = [
      makeRuleType({
        rule_type: 'max_duration',
        params: [makeRuleParam({ name: 'n', kind: 'integer', label: 'Count' })],
      }),
    ]
    const row = { rule_type: 'max_duration', params: { n: 'not-a-number' } }

    expect(ruleBrokenReason(types, row)).toBe('"Count" must be a number.')
  })

  it('flags an integer value below its declared minimum, naming the unit', () => {
    const types = [
      makeRuleType({
        rule_type: 'max_duration',
        params: [
          makeRuleParam({ name: 'n', kind: 'integer', label: 'Max duration', minimum: 5, unit: 'minutes' }),
        ],
      }),
    ]
    const row = { rule_type: 'max_duration', params: { n: 4 } }

    expect(ruleBrokenReason(types, row)).toBe('"Max duration" must be at least 5 minutes.')
  })

  it('flags a local_time value not stored as HH:MM:SS', () => {
    const types = [
      makeRuleType({
        rule_type: 'availability_hours',
        params: [makeRuleParam({ name: 'opens_at', kind: 'local_time', label: 'Opens' })],
      }),
    ]
    const row = { rule_type: 'availability_hours', params: { opens_at: '7am' } }

    expect(ruleBrokenReason(types, row)).toBe('"Opens" must be a time.')
  })
})
