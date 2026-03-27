# Install Layout & File Flow

How files move from a fresh `git clone` through `bootstrap.sh` to a running system.

## 1. Clone the Repo

```bash
cd ~
git clone https://github.com/user/reticulumPi.git
```

This creates `~/reticulumPi/` — a full git repo owned by the current user. This is the **source directory**.

## 2. Run Bootstrap

```bash
# Default install (copies to /opt/reticulumpi):
sudo bash scripts/bootstrap.sh --with-nomadnet

# Custom install directory:
sudo bash scripts/bootstrap.sh --install-dir /srv/reticulumpi --with-nomadnet

# In-place install (run directly from the cloned repo):
sudo bash scripts/bootstrap.sh --install-dir . --with-nomadnet
```

## 3. Path Resolution

Bootstrap resolves three key paths:

| Variable | Value | Source |
|---|---|---|
| `SCRIPT_DIR` | `/home/pi/reticulumPi/scripts` | Physical location of the script |
| `PROJECT_DIR` | `/home/pi/reticulumPi` | Parent of `scripts/` |
| `INSTALL_DIR` | `/opt/reticulumpi` (default) | `--install-dir` argument or default |

## 4. Copy Decision

Bootstrap takes one of three branches depending on the relationship between `PROJECT_DIR` and `INSTALL_DIR`:

### Branch A: In-place install (`INSTALL_DIR` equals `PROJECT_DIR`)

Triggered by `--install-dir .` (or any path that resolves to the repo itself).

**Nothing is copied.** The cloned repo becomes the install directory. Ownership is transferred to the `reticulumpi` service user. The calling user loses write access (they'd need `sudo` to edit files afterward).

### Branch B: Git repo already exists at `INSTALL_DIR`

Triggered when someone previously ran `git clone` directly into `/opt/reticulumpi`.

Runs `git pull` to update in place. No rsync.

### Branch C: rsync copy (default)

The most common path. Copies everything from the source repo to `INSTALL_DIR`, **excluding**:

- `.git/` — no version control history in the deployed copy
- `.venv/` — will be created fresh for the service user
- `__pycache__/`, `*.pyc` — stale bytecode from the dev machine
- `.ruff_cache/` — linter cache

After rsync, ownership is set to `reticulumpi:reticulumpi`.

**Result after this step:**

| Location | Contents | Owner | Has `.git`? |
|---|---|---|---|
| `~/reticulumPi/` | Full repo + git history | `pi:pi` | Yes |
| `/opt/reticulumpi/` | Same files, minus dev artifacts | `reticulumpi:reticulumpi` | No |

## 5. Python Venv

A virtual environment is created at `$INSTALL_DIR/.venv/`, owned by `reticulumpi`:

```bash
sudo -u reticulumpi python3 -m venv "$INSTALL_DIR/.venv"
sudo -u reticulumpi "$INSTALL_DIR/.venv/bin/pip" install -e "$INSTALL_DIR"
```

The `-e` (editable) install means Python links back to `$INSTALL_DIR/src/reticulumpi/` rather than copying into `site-packages`. This is why `update.sh` can sync new code without re-running `pip install`.

## 6. Config Files

Example configs are copied into system locations on first run only (won't overwrite existing files):

```
$INSTALL_DIR/config/reticulumpi/config.example.yaml
    -> /etc/reticulumpi/config.yaml

$INSTALL_DIR/config/reticulum/config.example
    -> /home/reticulumpi/.reticulum/config

$INSTALL_DIR/config/nomadnet/pages/*.mu
    -> /home/reticulumpi/.nomadnet/storage/pages/*.mu
```

## 7. Systemd Service Files

Source service files in the repo hardcode `/opt/reticulumpi`. Bootstrap templates them with `sed`, replacing that path with whatever `INSTALL_DIR` actually is:

```bash
sed "s|/opt/reticulumpi|$INSTALL_DIR|g" "$INSTALL_DIR/systemd/reticulumpi.service" \
    > /etc/systemd/system/reticulumpi.service
```

If you used the default `/opt/reticulumpi`, the sed is a no-op. If you used `--install-dir /srv/reticulumpi`, the installed service files would reference `/srv/reticulumpi/.venv/bin/...`.

## Final Directory Layout

After a default bootstrap (`--with-nomadnet`), the filesystem looks like this:

```
~/reticulumPi/                              <- user's git repo (untouched after bootstrap)
|-- .git/
|-- scripts/
|-- src/
|-- ...

/opt/reticulumpi/                           <- deployed copy (owned by reticulumpi)
|-- .venv/                                  <- fresh venv with rns, lxmf, nomadnet
|-- scripts/
|-- src/
|-- systemd/                                <- source templates (still say /opt/reticulumpi)
|-- config/                                 <- example configs (templates)
|-- ...
(no .git, no __pycache__, no .ruff_cache)

/etc/reticulumpi/
|-- config.yaml                             <- app config (plugins, identity, log level)

/home/reticulumpi/                          <- service user's home (data + runtime config)
|-- .reticulum/config                       <- Reticulum network config (interfaces, transport)
|-- .nomadnet/storage/pages/*.mu            <- NomadNet served pages
|-- .nomadnet-tui/                          <- TUI browse-only config (created by nomadnet-tui.sh)
|-- .config/reticulumpi/identity            <- node identity file (created at first run)
|-- .local/share/reticulumpi/lxmf/          <- LXMF message storage (created at runtime)

/etc/systemd/system/
|-- reticulumpi.service                     <- templated with actual INSTALL_DIR
|-- rnsd.service                            <- templated with actual INSTALL_DIR
```

## Updating

### If `INSTALL_DIR` is a git repo (Branch B or in-place install)

```bash
sudo bash /opt/reticulumpi/scripts/update.sh
```

`update.sh` auto-detects its install directory from its own location. If it finds a `.git` directory, it runs `git pull`, upgrades pip dependencies, re-templates service files if they changed, and restarts services.

### If `INSTALL_DIR` is an rsync copy (Branch C, the default)

The deployed copy at `/opt/reticulumpi` has no `.git`, so `update.sh` cannot pull new code on its own. The workflow is:

1. Pull changes in your source repo: `cd ~/reticulumPi && git pull`
2. Run update from the source repo: `sudo bash scripts/update.sh`

Or manually rsync then run update from the install dir:

```bash
sudo rsync -a --exclude='.git' --exclude='.venv' --exclude='__pycache__' \
    ~/reticulumPi/ /opt/reticulumpi/
sudo chown -R reticulumpi:reticulumpi /opt/reticulumpi
sudo bash /opt/reticulumpi/scripts/update.sh
```

## Security Model

The `reticulumpi` system user:

- Has `/usr/sbin/nologin` as its shell (no SSH/interactive login)
- Owns only `/opt/reticulumpi` (code), `/home/reticulumpi` (data), `/etc/reticulumpi` (config), `/var/lib/reticulumpi` (runtime)
- Has hardware groups only: `dialout`, `gpio`, `spi`, `i2c`
- Systemd sandboxing: `ProtectSystem=strict`, `ProtectHome=read-only`, `NoNewPrivileges=yes`, `PrivateTmp=yes`
- Only specific directories listed in `ReadWritePaths` are writable at runtime

The dev user's home directory is never accessed by the running services.
