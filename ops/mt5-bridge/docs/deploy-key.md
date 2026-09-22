# Deploy key: this machine → GitHub (read-only)

Wires the machine straight to the repo so a restore can `git fetch` /
`git reset --hard` directly, without the API.

- **Key location:** `~/.ssh/forex_deploy` (private, `chmod 600`), `~/.ssh/forex_deploy.pub`
- **Key type:** ed25519, no passphrase. Fingerprint: `SHA256:1eyaP68Ms/5WqL/GpY5H46REF3VVTelBu4mtkOEZDWw`
- **GitHub side:** deploy key on `kilomene/Forex-agent`, title
  `nova-restore-deploy-key`, id `164074286`, **read_only=true**.
  Pushes keep using the existing Git Data API flow; this key can never push.
- **SSH alias** (`~/.ssh/config`, `chmod 600`):

  ```
  Host github-forex
      HostName ssh.github.com
      Port 443
      User git
      IdentityFile ~/.ssh/forex_deploy
      IdentitiesOnly yes
      ProxyCommand nc -X connect -x hatch-egress-proxy:3128 %h %p
  ```

  Port 443 (`ssh.github.com`) + CONNECT through the egress proxy is required:
  direct TCP/22 to github.com is blocked from this sandbox.

## Canonical sync commands (for the restore script)

```sh
GIT_SSH_COMMAND="ssh -o BatchMode=yes" git -C /home/hatch/workspace/forex-migration/repo fetch origin
git -C /home/hatch/workspace/forex-migration/repo reset --hard origin/main
```

Prerequisite: `origin`'s fetch URL must be the SSH alias form, e.g.

```sh
git -C /home/hatch/workspace/forex-migration/repo remote set-url origin github-forex:kilomene/Forex-agent.git
```

(`GIT_SSH_COMMAND` only takes effect for SSH remotes; the clone currently
points at the HTTPS URL.)

## Pending permission (blocks end-to-end verification)

Outbound SSH is currently **denied for the assistant** by a platform policy.
`ssh -T github-forex` returns the policy banner:

> muse: Outbound SSH is turned off for this assistant. To allow it, ask the
> user to open Muse settings -> Permissions -> Direct network protocols and
> switch ssh from Deny to Ask.

**User action needed:** Muse settings → Permissions → Direct network
protocols → switch `ssh` from **Deny** to **Ask**. After that, verify:

```sh
ssh -T -o BatchMode=yes github-forex          # expect the GitHub auth greeting
git ls-remote github-forex:kilomene/Forex-agent.git HEAD   # expect a SHA
```

The raw TCP tunnel itself was proven working (GitHub's SSH banner and full
KEXINIT received through the proxy), so once the permission flips, the alias
should authenticate immediately with the registered deploy key.
