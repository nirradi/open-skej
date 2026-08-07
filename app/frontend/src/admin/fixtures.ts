/**
 * Shared fixtures for the admin dashboard tests.
 *
 * Test-only: nothing in the application imports this, and the bundler drops it.
 * It lives beside the components rather than under a `__fixtures__` directory so
 * that a drift between `Space` here and `Space` in `types.ts` is a compile error
 * in the same `tsc -b` that builds the app.
 *
 * The builders take overrides rather than being constants because almost every
 * assertion cares about exactly one field — `my_role`, `archived_at`, a
 * `status` — and spelling out the other five each time buries which one the test
 * is actually about.
 */

import type {
  AccessRequest,
  ApiConflict,
  ApiFailure,
  ApiForbidden,
  ApiOk,
  Invitation,
  Member,
  Resource,
  RuleDraftRead,
  RuleParamRead,
  RuleTypeRead,
  Space,
  SpaceRuleRead,
} from '../api'

export function ok<T>(data: T): ApiOk<T> {
  return { outcome: 'ok', data }
}

/**
 * The server's verbatim copy for refusing to unseat the final owner.
 *
 * Copied from `LAST_OWNER_DETAIL` in `app/backend/app/identity/router.py`. It is
 * duplicated here on purpose: the point of the tests that use it is that this
 * *specific sentence* reaches the screen instead of generic failure copy, and
 * asserting on a value the test itself invented would prove only that a string
 * survives a round trip. If the backend rewords it, these tests should be
 * updated deliberately, not silently follow along.
 */
export const LAST_OWNER_MESSAGE =
  'This Space must always have at least one owner.' +
  ' Promote another member to owner before changing this one.'

export function conflict(message: string = LAST_OWNER_MESSAGE): ApiConflict {
  return { outcome: 'conflict', message }
}

export function forbidden(message = "You don't have permission to do that."): ApiForbidden {
  return { outcome: 'forbidden', message }
}

export function failed(message = 'Something went wrong. Please try again.'): ApiFailure {
  return { outcome: 'failed', message }
}

export function makeSpace(overrides: Partial<Space> = {}): Space {
  return {
    public_id: 'sp_7f3a9c',
    name: 'Tennis Court',
    description: null,
    timezone: 'UTC',
    created_at: '2026-07-01T10:00:00.000Z',
    archived_at: null,
    my_role: 'owner',
    ...overrides,
  }
}

export function makeMember(overrides: Partial<Member> = {}): Member {
  return {
    user_id: 1,
    email: 'ada@example.com',
    name: 'Ada Lovelace',
    role: 'owner',
    created_at: '2026-07-01T10:00:00.000Z',
    ...overrides,
  }
}

export function makeAccessRequest(overrides: Partial<AccessRequest> = {}): AccessRequest {
  return {
    id: 10,
    user_id: 2,
    email: 'grace@example.com',
    name: 'Grace Hopper',
    status: 'pending',
    message: null,
    created_at: '2026-07-02T10:00:00.000Z',
    decided_at: null,
    decided_by_user_id: null,
    ...overrides,
  }
}

export function makeInvitation(overrides: Partial<Invitation> = {}): Invitation {
  return {
    id: 20,
    email: 'alan@example.com',
    role: 'member',
    status: 'pending',
    invited_by_user_id: 1,
    created_at: '2026-07-03T10:00:00.000Z',
    accepted_at: null,
    ...overrides,
  }
}

export function makeResource(overrides: Partial<Resource> = {}): Resource {
  return {
    id: 30,
    name: 'Court A',
    created_at: '2026-07-01T10:00:00.000Z',
    archived_at: null,
    ...overrides,
  }
}

/**
 * One `RuleParamRead` — the same five facts (`kind`/`label`/`unit`/`required`/
 * `minimum`) that serve both `RuleParamsForm`'s field and `ruleParamsAreValid`'s
 * own check of it.
 */
export function makeRuleParam(overrides: Partial<RuleParamRead> = {}): RuleParamRead {
  return {
    name: 'max_duration_minutes',
    kind: 'integer',
    label: 'Max duration',
    unit: 'minutes',
    required: true,
    minimum: 1,
    ...overrides,
  }
}

/**
 * A registered rule type, as `GET /rule-types` serves it. Defaults to a
 * `max_duration`-shaped type — one required integer parameter — since that is
 * the simplest real member of `rules.REGISTRY`. `SpaceRulesPage.test.tsx`
 * overrides `params` with an invented type to prove the page never branches on
 * `rule_type`.
 */
export function makeRuleType(overrides: Partial<RuleTypeRead> = {}): RuleTypeRead {
  return {
    rule_type: 'max_duration',
    label: 'Maximum booking duration',
    description: 'Refuses a booking longer than the configured duration.',
    priority: 30,
    reads_history: false,
    needs_local_resolution: false,
    is_single: false,
    params: [makeRuleParam()],
    ...overrides,
  }
}

/** One configured `space_rules` row, as `SpaceRuleRead` describes it. */
export function makeSpaceRule(overrides: Partial<SpaceRuleRead> = {}): SpaceRuleRead {
  return {
    id: 100,
    rule_type: 'max_duration',
    params: { max_duration_minutes: 90 },
    applies_to: null,
    enabled: true,
    created_at: '2026-07-01T10:00:00.000Z',
    updated_at: '2026-07-01T10:00:00.000Z',
    ...overrides,
  }
}

/**
 * One rule-generation job, as `GET`/`POST .../rule-drafts` serve it. Defaults
 * to a `queued` job with no attempts yet — the shape every job starts as.
 */
export function makeRuleDraft(overrides: Partial<RuleDraftRead> = {}): RuleDraftRead {
  return {
    id: 200,
    prompt: 'no booking may be longer than one hour',
    status: 'queued',
    attempts: [],
    error: null,
    generated_rule_type: null,
    human_code: null,
    created_at: '2026-07-01T10:00:00.000Z',
    updated_at: '2026-07-01T10:00:00.000Z',
    ...overrides,
  }
}
