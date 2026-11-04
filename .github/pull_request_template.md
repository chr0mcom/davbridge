## Summary

<!-- What does this change and why? Link the issue: Fixes #123 -->

## Type of change

- [ ] Bug fix
- [ ] New feature or behavior change
- [ ] Documentation only
- [ ] Build / CI / dependencies
- [ ] Refactoring or renaming (no functional change)

## Checklist

- [ ] Every commit carries a DCO sign-off (`git commit -s`), see CONTRIBUTING.md.
- [ ] Everything is in English (code, comments, messages, docs, commit messages).
- [ ] `pytest` passes locally, and I added or updated tests.
- [ ] `ruff check src tests tools` and `ruff format --check src tests tools` pass.
- [ ] I did not add real calendar data, real names/domains/e-mail addresses, credentials or captures of
      Microsoft clients or services; fixtures are synthetic.
- [ ] I did not paste text or schemas from Microsoft documents; new protocol behavior is cited in
      docs/protocol-sources.md.
- [ ] I updated docs/protocol-coverage.md and docs/limitations.md if protocol behavior changed.
- [ ] I updated CHANGELOG.md (section "Unreleased") for user-visible changes.
- [ ] The architecture rules still hold (one worker, no blocking I/O in handlers, no event content in the
      SQLite state, conditional writes, allow-listed logging).

## Notes for reviewers

<!-- Risks, migration steps, things you are unsure about. For protocol changes: which client and version did you test? -->
