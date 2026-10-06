# Contributing

Thanks for helping. This is a Python project; issues and pull requests are welcome.

## Set up and check your change

Needs Python 3.11+ (DuckDB is installed with the dev extras, as the reference engine). Caches and virtual environments stay in git-ignored folders inside the repo.

```bash
make setup   # install dependencies
make lint    # formatting, static checks
make test    # full test suite
make audit   # known-vulnerability check (as in CI)
make report  # every query with the optimizer off and on, and on DuckDB
```

A pull request is ready when `make lint` and `make test` pass and CI is green.

## Guidelines

- Keep each change focused; explain the problem it solves in the pull request.
- Add or update a test for every behaviour change, and update `docs/results/` when numbers change.
- Significant design changes need an ADR in `docs/adr/` (copy `0001` for the format).
- Keep function comments to one short phrase; let names and tests explain the rest.
- Report security problems privately, as described in [SECURITY.md](SECURITY.md).

By contributing you agree that your work is released under the [MIT License](LICENSE).
