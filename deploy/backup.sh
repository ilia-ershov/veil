#!/usr/bin/env bash
# =====================================================================================
#  Backup of everything that can't be recreated from git:
#    db.dump        the app database (pg_dump custom format, restore with pg_restore)
#    files.tar.gz   app secrets (.env), DKIM keys, SRS secret, Postfix/Caddy/OpenDKIM/SSH/fail2ban
#                   config, and the websites in /var/www (the personal page is not in git)
#    SHA256SUMS     checksums of both
#
#  Each run writes $BACKUP_DIR/<date>_<time>/, points $BACKUP_DIR/latest at it and deletes
#  runs older than $KEEP_DAYS days; a run that fails leaves nothing behind. Installed as a daily
#  systemd timer by `server-setup.sh backup`, which also mails root when a run fails; by hand:
#    sudo ./deploy/backup.sh
#
#  Copy to your computer (the backup holds secrets: keep it encrypted or offline):
#    scp -r root@SERVER:/var/backups/mail-organizer/latest/ ./mail-organizer-backup
#
#  Restore: see "Backups" in deploy/README.md.
# =====================================================================================
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/mail-organizer}"
KEEP_DAYS="${KEEP_DAYS:-14}"

# Paths outside the repo; missing ones are skipped (e.g. a dev machine without a mail server)
SYSTEM_FILES=(
  /etc/opendkim
  /etc/opendkim.conf
  /etc/postsrsd.secret
  /etc/default/postsrsd
  /etc/postfix/main.cf
  /etc/postfix/master.cf
  /etc/postfix/virtual
  /etc/postfix/pgsql-aliases.cf
  /etc/aliases
  /etc/caddy/Caddyfile
  /etc/fail2ban/jail.local
  /etc/ssh/sshd_config.d
  /var/www
)
REPO_FILES=(.env docker-compose.override.yml deploy/server.env)

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
umask 077
mkdir -p "$BACKUP_DIR"
run="$BACKUP_DIR/$(date +%F_%H%M)"
mkdir "$run"   # never reuses a directory: a second run in the same minute fails instead
# A run that stops half-way removes its directory: no empty or partial backups next to good ones.
trap '[ -f "$run/SHA256SUMS" ] || rm -rf "$run"' EXIT
cd "$REPO_DIR"

# 1. Database: dump inside the container (same pg_dump version as the server), then check it reads back
docker compose exec -T db pg_dump -U mail_organizer -d mail_organizer --format=custom > "$run/db.dump.tmp"
docker compose exec -T db pg_restore --list < "$run/db.dump.tmp" > /dev/null
mv "$run/db.dump.tmp" "$run/db.dump"

# 2. Secrets and config (absolute paths are stored without the leading slash)
files=()
for f in "${REPO_FILES[@]}"; do [ -e "$f" ] && files+=("$REPO_DIR/$f"); done
for f in "${SYSTEM_FILES[@]}"; do [ -e "$f" ] && files+=("$f"); done
tar -czf "$run/files.tar.gz" --absolute-names --transform 's|^/||' "${files[@]}"

(cd "$run" && sha256sum db.dump files.tar.gz > SHA256SUMS)
ln -sfn "$run" "$BACKUP_DIR/latest"

# 3. Rotation: only directories this script created (YYYY-MM-DD_HHMM)
find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d -name '20[0-9][0-9]-[0-9][0-9]-[0-9][0-9]_[0-9][0-9][0-9][0-9]' \
  -mtime +"$KEEP_DAYS" -exec rm -rf {} +

echo "backup: $run ($(du -sh "$run" | cut -f1))"
