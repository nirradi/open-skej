import type { RuleTypeRead } from '../api'

/**
 * The client-side half of surfacing 6.6's fail-closed path: a rule whose
 * stored `params` no longer satisfy its own type's declared schema is a
 * denial the server enforces silently — every booking against the Space is
 * refused, with nothing in the product to say why — so this is the only
 * place an admin can see the reason at all. `SpaceRulesPage` calls
 * `ruleBrokenReason` for every configured row and renders the result as a
 * visible banner rather than pretending the row is fine.
 *
 * The registry (`GET /rule-types`) is fetched once per page load and handed
 * in here rather than re-fetched per row — this module holds no state of its
 * own, only the two checks: does the row's `rule_type` still name a
 * registered type, and if so do its `params` still satisfy that type's
 * schema.
 */

/** The registered type a row names, or `undefined` if it names nothing real. */
export function findRuleTypeFor(
  ruleTypes: RuleTypeRead[],
  ruleType: string,
): RuleTypeRead | undefined {
  return ruleTypes.find((candidate) => candidate.rule_type === ruleType)
}

/**
 * Why a stored row is broken, or `null` if it is fine.
 *
 * Two independent ways a row can be broken, checked in order: an unregistered
 * `rule_type` (the type was renamed or removed from the registry since the
 * row was written — the registry's own docstring names renaming the class,
 * never the stable string id, as exactly the case this guards against), or a
 * `rule_type` that still exists but whose stored `params` no longer satisfy
 * its schema — a required parameter missing, or a value below its declared
 * `minimum`. Both are the admin-visible half of the same server-side
 * refusal: `app.identity.service` raises `InvalidRuleParamsError` /
 * `UnknownRuleTypeError` for the identical conditions when *writing* a row,
 * but nothing re-validates a row already sitting in the database, so this
 * check is what a stale row's failure looks like from the outside.
 */
export function ruleBrokenReason(
  ruleTypes: RuleTypeRead[],
  row: { rule_type: string; params: Record<string, unknown> },
): string | null {
  const ruleType = findRuleTypeFor(ruleTypes, row.rule_type)
  if (!ruleType) {
    return `"${row.rule_type}" is not a registered rule type.`
  }

  for (const param of ruleType.params) {
    const raw = row.params[param.name]

    if (raw === undefined || raw === null) {
      if (param.required) {
        return `Missing required parameter "${param.label}".`
      }
      continue
    }

    // `"integer"` and `"local_time"` are identical in storage — `kind` only ever
    // picks the eventual widget (`rules/rules/registry.py`'s `ParamKind`
    // docstring) — so both get the same numeric check here.
    if (param.kind === 'integer' || param.kind === 'local_time') {
      if (typeof raw !== 'number' || !Number.isFinite(raw)) {
        return `"${param.label}" must be a number.`
      }
      if (param.minimum !== null && raw < param.minimum) {
        const unit = param.unit ? ` ${param.unit}` : ''
        return `"${param.label}" must be at least ${param.minimum}${unit}.`
      }
    }
  }

  return null
}
