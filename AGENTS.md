# Repository instructions

<!-- postman-cli:installation:start -->
Postman CLI is installed on this device, and can be used for API Engineering work.
<!-- postman-cli:installation:end -->

## GitHub authentication

Use the GitHub account authorized by the current user and session. Verify the
active login before pushing branches, editing PRs, or running deployment-related
GitHub commands. Do not switch to a contributor's personal account merely
because their changes are being reviewed or released. Preserve the configured
commit author name/email unless the user requests an authorship change.

## Live testing budget

All paid AI/provider testing must use the persistent lifetime ledger in
`/Users/sawarn/.config/weaves-testing/` through the budget-gated tenancy harness.
The user's exhaustive lifetime authorization is USD 100 total across runs and
models, not USD 100 per run or repository. Reserve before dispatch; retain holds
for uncertain charges. Never reset/delete the ledger, bypass its relay, or raise
the limit automatically. Stop and ask the user for more budget when exhausted.
Do not print keys or put them in Git, shell arguments, logs, or CI artifacts.
