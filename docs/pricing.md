# Pricing assumptions

## Catalogs

A catalog is a JSON document validated by `PriceCatalog`. Two ship with the repository:

| file | kind | use |
| --- | --- | --- |
| `src/aiecon/data/demo_support_v1/synthetic-catalog.json` | `synthetic` | the offline demo; `fixture-*` models with artificial rates |
| `catalogs/live-demo.json` | `real` | verified public prices for `gpt-5-nano`, `claude-haiku-4-5-20251001` (alias `claude-haiku-4-5`) and `claude-sonnet-5`, retrieved 2026-09-26 |

A synthetic workspace refuses a real catalog and a live workspace refuses a synthetic one.

Every price record carries `catalog_version, provider, model_id, api_family, resource,
service_tier, region, context_band, effective_from_ms, effective_to_ms, currency, unit,
unit_quantity, unit_price, source_url, retrieved_at, effective_date_basis`.

- `effective_date_basis` is `provider_announced` or `observed_at_retrieval`. Unknown history
  is never back-filled: the live catalog's prices are effective from the day they were read.
- Aliases are explicit (`claude-haiku-4-5` -> `claude-haiku-4-5-20251001`) with a source.
  An unknown model is never priced with a "similar" model's rate.
- `cache_contracts` state which write tiers exist for a model, which cache policies it
  supports and its minimum cacheable prefix.

## Resources

`input_uncached`, `input_cache_read`, `input_cache_write` (single tier),
`input_cache_write_5m`, `input_cache_write_1h`, `output`, `request`. A token belongs to
exactly one input class. Reasoning tokens are part of `output` and are never added again.

## Estimation

`estimate(call, catalog, pricing_run_id)` is a pure function:

```
line_cost           = quantity * unit_price / unit_quantity      (12 decimal places)
call_estimated_cost = sum of mutually exclusive priced line items
```

The price version is chosen by the call's start time (UTC). A call that starts on one UTC
day and ends on the next is flagged `boundary_call` for reconciliation diagnostics.

When something cannot be priced without guessing, the estimator emits an *unpriced* line
item with a reason instead of a number, and the call's `cost_complete` becomes false:

| reason | when |
| --- | --- |
| `usage_missing` / `usage_invalid` | no usage, or internally inconsistent usage |
| `unknown_model` | model id not in the catalog (after explicit alias lookup) |
| `timestamp_missing` | no start or end time to select a price version |
| `input_split_unknown` | total input known but the cached / uncached split is not |
| `price_missing` | the resource, tier, service tier or region has no price |
| `cache_write_tier_unknown` | Anthropic write total without the 5m/1h breakdown |
| `cache_write_tier_unsupported` | a tier label the catalog does not know |
| `cache_write_count_unknown` | OpenAI write counter absent on a model that bills writes; the rest is a lower bound |
| `output_unknown` | output tokens missing |

Reports show `known_cost_subtotal` with `cost_complete=false` in that case; the subtotal is
never called a total.

## Pricing runs

`pricing_run_id` is derived from the catalog hash, the hash of the call projections and the
normalizer version. Re-running the same inputs produces the same id and replaces its line
items; different runs are never added together. The catalog document itself is persisted
with the run so later analysis uses exactly the prices the estimate used.

## Sample (PLAN.md 13.1)

Rates per million tokens: input 2, cache read 0.5, cache write 2.5, output 8.
OpenAI shape `total=1000, read=600, write=100, output=100` and Anthropic shape
`uncached=300, read=600, write=100, output=100` both price to
`(300 x 2 + 600 x 0.5 + 100 x 2.5 + 100 x 8) / 1,000,000 = $0.00195`.
