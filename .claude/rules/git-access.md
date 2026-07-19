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
* `gh` CLI is separately authenticated and already active as `nirradi`, so `gh pr create` etc. work
  without the alias. Only **git transport** (push/fetch) needs it.
* Workflow files under `.github/workflows/` push fine over SSH. The `gh` OAuth token lacks the
  `workflow` scope, so do **not** fall back to an HTTPS remote — that would reintroduce the block.
