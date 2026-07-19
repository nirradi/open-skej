# Git & GitHub Access

## The repo is `nirradi/open-skej` — push as `nirradi`, not `optinirr`

This machine has **two** GitHub accounts configured. Only `nirradi` has write access to this repo.

The default SSH key (`~/.ssh/id_ed25519`) authenticates as **`optinirr`**, which does **not** have
push access. A plain `git@github.com:...` remote therefore fails with:

```
ERROR: Permission to nirradi/open-skej.git denied to optinirr.
```

## The fix (already applied)

`~/.ssh/config` defines a host alias that selects the correct key:

```
Host github-nirradi
  HostName github.com
  User git
  IdentityFile ~/.ssh/nirradigit
  IdentitiesOnly yes
```

The `origin` remote must use that alias, **not** `github.com`:

```
git@github-nirradi:nirradi/open-skej.git
```

Verify with `git remote -v`. If it ever reads `git@github.com:nirradi/open-skej.git`, restore it:

```
git remote set-url origin git@github-nirradi:nirradi/open-skej.git
```

Confirm which account a key authenticates as with `ssh -T git@github-nirradi` (expect `Hi nirradi!`).

## Rules for agents

* **Never** rewrite `origin` back to a bare `github.com` URL — it silently breaks pushing.
* Clone/add remotes using the `github-nirradi` alias.
* The `gh` CLI is authenticated for **both** accounts and its active account is global, machine-wide
  state that can be left on `optinirr`. If `gh pr create` fails with `must be a collaborator`, the
  active account is wrong. Check and fix with:

  ```
  gh auth status          # look for "Active account: true"
  gh auth switch --user nirradi
  ```

  Do this before any `gh` write operation (`pr create`, `pr merge`, `api -X POST`). Note this is
  separate from the SSH alias above: **git transport** needs the alias, **gh** needs the active
  account. Both must point at `nirradi`.
* Workflow files under `.github/workflows/` push fine over SSH. The `gh` OAuth token lacks the
  `workflow` scope, so do **not** fall back to an HTTPS remote — that would reintroduce the block.
