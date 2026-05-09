# Contributing to PyroNMS

## Workflow
1. Create a branch from `main`.
2. Keep changes scoped and tested.
3. Open a pull request with summary and screenshots for UI changes.

## Commit Style
- Use clear messages:
  - `fix(api): ...`
  - `feat(web): ...`
  - `docs: ...`

## Checks Before PR
- API starts without syntax errors.
- Core pages load (`index`, `admin`, `settings`).
- Mobile layout is validated for ONT list and sidebar.

## Security
- Do not commit credentials, tokens, or private customer data.
- Use `.bak-*` runtime backups only on server, not in Git.
