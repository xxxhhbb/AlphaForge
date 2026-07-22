## Outcome

<!-- Lead with the user/system result, not a file list. -->

-

## Why

<!-- What problem or acceptance gap does this close? Link: Closes #123. -->

## Scope

<!-- Key changes and affected product path. -->

-

## Non-goals

<!-- What is deliberately not implemented? -->

-

## Authority and risk

- [ ] No Evidence/PIT/Promotion/Risk/Sandbox authority change
- [ ] No Broker/order/account/OAuth/secret/deployment change
- [ ] No new network access
- [ ] Auxiliary research is not presented as official Evidence
- [ ] Partial/blocked/failed/cancelled states remain fail-closed

<!-- If any box cannot be checked, explain the boundary, tests and rollback. -->

## Verification

<!-- Exact commands and exact results. Distinguish passed/failed/skipped/not run. -->

- [ ] Relevant Python tests
- [ ] `cd frontend && npm run test:run`
- [ ] `cd frontend && npm run build`
- [ ] `python scripts/check_alphaforge_docs.py`

```text
Command:
Result:
```

## UI evidence

<!-- For UI changes: desktop and 360px screenshots, keyboard/accessibility notes. -->

## Data/model/network impact

<!-- Provider calls, datasets, frozen identities, cost/budget, or explicitly "none". -->

## Rollback

<!-- Safe revert or feature-disable path. -->

## Checklist

- [ ] Product branding uses AlphaForge（千因研究院）
- [ ] Existing user changes were preserved
- [ ] No hardcoded keys, personal paths, or private artifacts
- [ ] Tests cover failure/negative states, not only happy paths
- [ ] User/API/AI documentation is updated
- [ ] Commit(s) include DCO `Signed-off-by:`
- [ ] Known limitations are stated honestly
