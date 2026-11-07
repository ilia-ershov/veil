#!/usr/bin/env bash
# =====================================================================================
#  Full server setup for the Mail Organizer: site, HTTPS, mail server, hardening.
#
#  STATUS: written to reproduce the production server as configured by hand on
#  2026-10-19 (Debian 13). This script as a whole has NOT been run end to end.
#  Every step mirrors a command that was run and verified manually, but read the
#  step before running it and check the output. It is meant to be readable by a
#  human or an AI assistant as an exact description of the setup.
#
#  What it sets up (each step is a function below):
#    1. packages            Docker, Caddy, Postfix (+pgsql), postsrsd, OpenDKIM, unbound,
#                           SPF policy daemon, fail2ban, unattended-upgrades
#    2. app                 .env + docker-compose.override.yml, `docker compose up`
#    3. lookup_role         read-only DB role Postfix uses to resolve aliases
#    4. site                personal page for https://$DOMAIN
#    5. caddy               HTTPS for $DOMAIN, www, $SERVICE_DOMAIN, $MAIL_HOST
#    6. postfix             receive for both domains, forward, anti-relay, anti-spam
#    7. srs                 postsrsd: forwarded mail keeps passing SPF
#    8. dkim                OpenDKIM keys + signing of the server's own mail
#    9. resolver            local unbound (needed for Spamhaus lookups)
#   10. postfix_tls         use Caddy's Let's Encrypt certificate for SMTP TLS
#   11. fail2ban            ban SSH / SMTP brute force
#   12. updates             automatic security updates
#   13. ssh                 key-only SSH (only if a key is installed)
#   14. backup              daily backup of the database, secrets and config (deploy/backup.sh);
#                           a failed run is mailed to root
#   15. summary             DNS records to create, what to ask the hosting provider
#
#  Usage:
#    cp deploy/server.env.example deploy/server.env   # edit values
#    sudo ./deploy/server-setup.sh                    # all steps
#    sudo ./deploy/server-setup.sh caddy postfix      # only some steps
#
#  Things no script can do (see deploy/README.md):
#    - buy the domain and create the DNS records printed by the `summary` step
#    - ask the hosting provider to set the PTR (reverse DNS) of SERVER_IP to MAIL_HOST
#
#  Moving to a new server: restore the latest backup made by deploy/backup.sh
#  (DKIM keys, SRS secret, .env, database), see "Backups" in deploy/README.md.
# =====================================================================================
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$DEPLOY_DIR")"
CONFIG="$DEPLOY_DIR/server.env"

log()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!!  %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31mxx  %s\033[0m\n' "$*" >&2; exit 1; }
random_secret() { python3 -c "import secrets; print(secrets.token_urlsafe($1))"; }
backup_once() { [ -e "$1" ] && [ ! -e "$1.orig" ] && cp -a "$1" "$1.orig" || true; }   # keep the distro original

[ "$(id -u)" -eq 0 ] || die "run as root"
[ -f "$CONFIG" ] || die "create $CONFIG from server.env.example first"
# shellcheck source=/dev/null
source "$CONFIG"
: "${DOMAIN:?}" "${SERVICE_SUBDOMAIN:?}" "${SERVER_IP:?}" "${FORWARD_TO:?}" "${ACME_EMAIL:?}" "${APP_SUBNET:?}"
PERSONAL_ADDRESSES="${PERSONAL_ADDRESSES:-}"
F2B_IGNORE="${F2B_IGNORE:-}"
SSH_KEY_ONLY="${SSH_KEY_ONLY:-no}"

SERVICE_DOMAIN="$SERVICE_SUBDOMAIN.$DOMAIN"   # e.g. veil.ershov.me
MAIL_HOST="mail.$DOMAIN"                      # e.g. mail.ershov.me
DB_PORT=5433                                  # app database, published on 127.0.0.1 for Postfix only
APP_PORT=8080                                 # app (nginx container), published on 127.0.0.1 for Caddy only
export DEBIAN_FRONTEND=noninteractive


# -------------------------------------------------------------------------------------
# 1. Packages
# -------------------------------------------------------------------------------------
step_packages() {
  log "1. Installing packages"
  # Postfix asks questions during installation: answer them in advance
  echo "postfix postfix/main_mailer_type select Internet Site" | debconf-set-selections
  echo "postfix postfix/mailname string $DOMAIN" | debconf-set-selections
  apt-get update -q
  apt-get install -y -q \
    ca-certificates curl git python3 dnsutils \
    docker.io docker-compose \
    caddy \
    postfix postfix-pgsql postfix-policyd-spf-python postsrsd \
    opendkim opendkim-tools \
    unbound \
    fail2ban unattended-upgrades apt-listchanges
  systemctl enable --now docker
}


# -------------------------------------------------------------------------------------
# 2. The application (Docker Compose: PostgreSQL, Redis, API, nginx with the SPA)
# -------------------------------------------------------------------------------------
step_app() {
  log "2. Application ($SERVICE_DOMAIN)"
  cd "$REPO_DIR"

  if [ ! -f .env ]; then
    # Secrets are generated once; keep this file (and back it up) — it is not in git.
    umask 077
    cat > .env <<EOF
SECRET_KEY=$(random_secret 48)
POSTGRES_PASSWORD=$(random_secret 24)
MAIL_DOMAIN=$SERVICE_DOMAIN
HTTP_BIND=127.0.0.1
HTTP_PORT=$APP_PORT
# Caddy -> nginx -> API: two proxies in front of the API
TRUST_PROXY=2
# codes and notifications go out through the host's Postfix
MAIL_BACKEND=smtp
MAIL_FROM=no-reply@$SERVICE_DOMAIN
SMTP_HOST=host.docker.internal
SMTP_PORT=25
SMTP_STARTTLS=false
SMTP_SSL=false
EOF
    umask 022
  else
    warn ".env exists, keeping it"
  fi

  # Server-specific wiring (git-ignored): DB port for Postfix, host access for sending mail,
  # and a FIXED subnet — Postfix trusts exactly this subnet (mynetworks), nothing broader.
  cat > docker-compose.override.yml <<EOF
services:
  db:
    ports: ["127.0.0.1:$DB_PORT:5432"]
  backend:
    extra_hosts: ["host.docker.internal:host-gateway"]

networks:
  default:
    ipam:
      config:
        - subnet: $APP_SUBNET
EOF
  docker compose up -d --build --wait
  curl -fsS "http://127.0.0.1:$APP_PORT/api/health" && echo
}


# -------------------------------------------------------------------------------------
# 3. Read-only database role for Postfix
# -------------------------------------------------------------------------------------
step_lookup_role() {
  log "3. Postfix lookup role in the app database"
  cd "$REPO_DIR"
  local cf=/etc/postfix/pgsql-aliases.cf password
  # reuse the existing password if the file is already there, so re-running is harmless
  if [ -f "$cf" ] && grep -q '^password = ' "$cf"; then
    password=$(sed -n 's/^password = //p' "$cf")
  else
    password=$(random_secret 24)
  fi

  local psql=(docker compose exec -T db psql -v ON_ERROR_STOP=1 -q -U mail_organizer -d mail_organizer)
  if [ "$("${psql[@]}" -Atc "SELECT 1 FROM pg_roles WHERE rolname='postfix_lookup'")" = "1" ]; then
    "${psql[@]}" -c "ALTER ROLE postfix_lookup PASSWORD '$password'"
  else
    # can see 5 columns only: no password hashes, no 2FA secrets, no writes
    sed "s/'change-me'/'$password'/" "$DEPLOY_DIR/postfix/lookup-role.sql" | "${psql[@]}"
  fi

  mkdir -p /etc/postfix
  cat > "$cf" <<EOF
# Resolves <alias>@$SERVICE_DOMAIN to the owner's main address. Deactivated aliases return
# nothing, so Postfix rejects mail for them at SMTP time ("User unknown").
hosts = 127.0.0.1:$DB_PORT
user = postfix_lookup
password = $password
dbname = mail_organizer
domain = $SERVICE_DOMAIN
query = SELECT u.email FROM aliases a JOIN users u ON u.uuid = a.user_id WHERE a.local_part = '%u' AND a.is_active
EOF
  chown root:postfix "$cf" 2>/dev/null || true
  chmod 640 "$cf"
}


# -------------------------------------------------------------------------------------
# 4. Personal site
# -------------------------------------------------------------------------------------
step_site() {
  log "4. Personal site (https://$DOMAIN)"
  mkdir -p "/var/www/$DOMAIN"
  if [ ! -f "/var/www/$DOMAIN/index.html" ]; then
    cp "$DEPLOY_DIR/site/index.html" "/var/www/$DOMAIN/index.html"
  fi
  mkdir -p "/var/www/$DOMAIN/fonts"
  cp "$DEPLOY_DIR"/site/fonts/* "/var/www/$DOMAIN/fonts/"
  chmod -R a+rX "/var/www/$DOMAIN"
}


# -------------------------------------------------------------------------------------
# 5. Caddy: HTTPS with automatic Let's Encrypt certificates
# -------------------------------------------------------------------------------------
step_caddy() {
  log "5. Caddy"
  backup_once /etc/caddy/Caddyfile
  cat > /etc/caddy/Caddyfile <<EOF
# HTTPS for $DOMAIN and the mail organizer. Certificates are issued and renewed automatically
# (they can only be issued once the A records point at this server).
{
	email $ACME_EMAIL
}

(security) {
	header {
		Strict-Transport-Security "max-age=31536000"
		-Server
	}
}

# personal site
$DOMAIN {
	import security
	header {
		X-Content-Type-Options nosniff
		X-Frame-Options DENY
		Referrer-Policy no-referrer
		Content-Security-Policy "default-src 'none'; style-src 'unsafe-inline'; font-src 'self'; img-src data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
	}
	root * /var/www/$DOMAIN
	file_server
	encode gzip
}

www.$DOMAIN {
	redir https://$DOMAIN{uri} permanent
}

# the mail organizer (nginx in Docker; it adds its own security headers)
$SERVICE_DOMAIN {
	import security
	reverse_proxy 127.0.0.1:$APP_PORT
}

# the mail server's name: its certificate is also used by Postfix (step 10)
$MAIL_HOST {
	import security
	redir https://$SERVICE_DOMAIN permanent
}
EOF
  caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
  systemctl enable caddy
  systemctl reload-or-restart caddy
}


# -------------------------------------------------------------------------------------
# 6. Postfix: receive mail for both domains, forward it, refuse everything else
# -------------------------------------------------------------------------------------
step_postfix() {
  log "6. Postfix"
  backup_once /etc/postfix/main.cf
  backup_once /etc/postfix/master.cf

  # Mail for root (cron, updates, failed backups) goes to the first personal address. Postfix
  # completes "root" to root@$DOMAIN (myorigin), a virtual alias domain, so the entry has to be in
  # /etc/postfix/virtual: /etc/aliases only covers root@localhost.
  local root_target="${PERSONAL_ADDRESSES%% *}@$DOMAIN"
  [ -n "${PERSONAL_ADDRESSES}" ] || root_target="$FORWARD_TO"

  # Personal and role addresses. The service's aliases come from the database (step 3).
  {
    echo "# Personal and role addresses (aliases of the service are looked up in the database)"
    for local in $PERSONAL_ADDRESSES postmaster abuse; do
      echo "$local@$DOMAIN    $FORWARD_TO"
    done
    echo "root@$DOMAIN    $root_target"
    echo "postmaster@$SERVICE_DOMAIN    $FORWARD_TO"
    echo "abuse@$SERVICE_DOMAIN    $FORWARD_TO"
  } > /etc/postfix/virtual
  postmap /etc/postfix/virtual

  if grep -q '^root:' /etc/aliases; then
    sed -i "s|^root:.*|root: $root_target|" /etc/aliases
  else
    echo "root: $root_target" >> /etc/aliases
  fi
  newaliases

  postconf -e \
    "myhostname = $MAIL_HOST" \
    "mydomain = $DOMAIN" \
    "myorigin = \$mydomain" \
    "mydestination = localhost" \
    "inet_interfaces = all" \
    "inet_protocols = ipv4" \
    "mynetworks_style = host" \
    "mynetworks = 127.0.0.0/8 $APP_SUBNET" \
    "virtual_alias_domains = $DOMAIN $SERVICE_DOMAIN" \
    "virtual_alias_maps = hash:/etc/postfix/virtual, pgsql:/etc/postfix/pgsql-aliases.cf" \
    "smtpd_banner = \$myhostname ESMTP" \
    "smtpd_helo_required = yes" \
    "disable_vrfy_command = yes" \
    "message_size_limit = 26214400" \
    "smtpd_relay_restrictions = permit_mynetworks, reject_unauth_destination" \
    "smtpd_helo_restrictions = permit_mynetworks, reject_invalid_helo_hostname, reject_non_fqdn_helo_hostname" \
    "smtpd_sender_restrictions = permit_mynetworks, reject_non_fqdn_sender, reject_unknown_sender_domain" \
    "smtpd_recipient_restrictions = permit_mynetworks, reject_unauth_destination, reject_non_fqdn_recipient, reject_unknown_recipient_domain, check_policy_service unix:private/policyd-spf" \
    "smtpd_data_restrictions = reject_unauth_pipelining" \
    "policyd-spf_time_limit = 3600" \
    "smtpd_tls_security_level = may" \
    "smtpd_tls_protocols = >=TLSv1.2" \
    "smtpd_tls_mandatory_protocols = >=TLSv1.2" \
    "smtpd_tls_loglevel = 1" \
    "smtp_tls_security_level = may" \
    "smtp_tls_protocols = >=TLSv1.2" \
    "smtp_tls_loglevel = 1" \
    `# postscreen: drop bots listed by Spamhaus/SpamCop and clients that talk before the greeting` \
    "postscreen_access_list = permit_mynetworks" \
    "postscreen_blacklist_action = drop" \
    "postscreen_greet_action = enforce" \
    "postscreen_dnsbl_action = enforce" \
    "postscreen_dnsbl_threshold = 3" \
    "postscreen_dnsbl_sites = zen.spamhaus.org=127.0.0.[2..11]*3, bl.spamcop.net=127.0.0.2*2, list.dnswl.org=127.0.[0..255].[1..3]*-2" \
    "postscreen_dnsbl_whitelist_threshold = -1"

  # master.cf: postscreen in front of smtpd on port 25, plus the SPF policy service
  postconf -Me "smtp/inet = smtp inet n - y - 1 postscreen"
  postconf -Me "smtpd/pass = smtpd pass - - y - - smtpd"
  postconf -Me "dnsblog/unix = dnsblog unix - - y - 0 dnsblog"
  postconf -Me "tlsproxy/unix = tlsproxy unix - - y - 0 tlsproxy"
  postconf -Me "policyd-spf/unix = policyd-spf unix - n n - 0 spawn user=policyd-spf argv=/usr/bin/policyd-spf"

  # SPF check of incoming mail: reject only hard failures (-all); never check the app's own subnet
  local spf=/etc/postfix-policyd-spf-python/policyd-spf.conf
  backup_once "$spf"
  sed -i "s|^skip_addresses = .*|skip_addresses = 127.0.0.0/8,::ffff:127.0.0.0/104,::1,$APP_SUBNET|" "$spf"
  grep -q '^HELO_reject = Fail' "$spf" || sed -i 's|^HELO_reject = .*|HELO_reject = Fail|' "$spf"
  grep -q '^Mail_From_reject = Fail' "$spf" || sed -i 's|^Mail_From_reject = .*|Mail_From_reject = Fail|' "$spf"

  postfix check
  systemctl enable postfix
  systemctl restart postfix
}


# -------------------------------------------------------------------------------------
# 7. SRS: rewrite the envelope sender of forwarded mail so it passes SPF at the destination
# -------------------------------------------------------------------------------------
step_srs() {
  log "7. postsrsd (SRS)"
  local conf=/etc/default/postsrsd
  backup_once "$conf"
  sed -i "s|^SRS_DOMAIN=.*|SRS_DOMAIN=$DOMAIN|" "$conf"
  if grep -q '^SRS_EXCLUDE_DOMAINS=' "$conf"; then
    sed -i "s|^SRS_EXCLUDE_DOMAINS=.*|SRS_EXCLUDE_DOMAINS=$DOMAIN,$SERVICE_DOMAIN|" "$conf"
  else
    echo "SRS_EXCLUDE_DOMAINS=$DOMAIN,$SERVICE_DOMAIN" >> "$conf"
  fi
  systemctl enable postsrsd
  systemctl restart postsrsd

  postconf -e \
    "sender_canonical_maps = tcp:127.0.0.1:10001" \
    "sender_canonical_classes = envelope_sender" \
    "recipient_canonical_maps = tcp:127.0.0.1:10002" \
    "recipient_canonical_classes = envelope_recipient, header_recipient"
  systemctl reload postfix
}


# -------------------------------------------------------------------------------------
# 8. DKIM: sign mail the server itself sends (codes, notifications), not forwarded mail
# -------------------------------------------------------------------------------------
step_dkim() {
  log "8. OpenDKIM"
  for d in "$DOMAIN" "$SERVICE_DOMAIN"; do
    mkdir -p "/etc/opendkim/keys/$d"
    if [ ! -f "/etc/opendkim/keys/$d/mail.private" ]; then
      opendkim-genkey -b 2048 -d "$d" -D "/etc/opendkim/keys/$d" -s mail
    else
      warn "DKIM key for $d exists, keeping it (DNS record stays valid)"
    fi
  done
  cat > /etc/opendkim/signing.table <<EOF
*@$DOMAIN         mail._domainkey.$DOMAIN
*@$SERVICE_DOMAIN  mail._domainkey.$SERVICE_DOMAIN
EOF
  cat > /etc/opendkim/key.table <<EOF
mail._domainkey.$DOMAIN         $DOMAIN:mail:/etc/opendkim/keys/$DOMAIN/mail.private
mail._domainkey.$SERVICE_DOMAIN  $SERVICE_DOMAIN:mail:/etc/opendkim/keys/$SERVICE_DOMAIN/mail.private
EOF
  cat > /etc/opendkim/trusted.hosts <<EOF
# Only mail from these hosts is signed: the server itself and the app's Docker network
127.0.0.1
localhost
$APP_SUBNET
EOF
  backup_once /etc/opendkim.conf
  cat > /etc/opendkim.conf <<'EOF'
# DKIM signing for the server's own outgoing mail
Syslog			yes
SyslogSuccess		yes
Mode			s
Canonicalization	relaxed/simple
OversignHeaders		From
UserID			opendkim
UMask			007
# TCP socket: Postfix's smtpd runs in a chroot and cannot reach /run
Socket			inet:8891@127.0.0.1
PidFile			/run/opendkim/opendkim.pid
TrustAnchorFile		/usr/share/dns/root.key
KeyTable		refile:/etc/opendkim/key.table
SigningTable		refile:/etc/opendkim/signing.table
InternalHosts		refile:/etc/opendkim/trusted.hosts
ExternalIgnoreList	refile:/etc/opendkim/trusted.hosts
EOF
  chown -R opendkim:opendkim /etc/opendkim
  chmod 700 /etc/opendkim/keys/*
  chmod 600 /etc/opendkim/keys/*/mail.private

  # Debian generates the systemd unit's socket from /etc/default/opendkim
  sed -i 's|^SOCKET=.*|SOCKET=inet:8891@127.0.0.1|' /etc/default/opendkim
  if [ -x /lib/opendkim/opendkim.service.generate ]; then
    /lib/opendkim/opendkim.service.generate
    systemctl daemon-reload
  fi
  systemctl enable opendkim
  systemctl restart opendkim

  postconf -e \
    "milter_default_action = accept" \
    "milter_protocol = 6" \
    "smtpd_milters = inet:127.0.0.1:8891" \
    "non_smtpd_milters = inet:127.0.0.1:8891"
  systemctl reload postfix
}


# -------------------------------------------------------------------------------------
# 9. Local DNS resolver: Spamhaus does not answer queries that come through 8.8.8.8 / 1.1.1.1
# -------------------------------------------------------------------------------------
step_resolver() {
  log "9. unbound (local resolver, 127.0.0.1 only — port 53 is NOT opened to the internet)"
  cat > /etc/unbound/unbound.conf.d/local-only.conf <<'EOF'
# Local recursive resolver for this server only (DNSBL lookups need a non-public resolver)
server:
    interface: 127.0.0.1
    access-control: 127.0.0.0/8 allow
    access-control: 0.0.0.0/0 refuse
    do-ip6: no
    hide-identity: yes
    hide-version: yes
EOF
  unbound-checkconf
  systemctl enable unbound
  systemctl restart unbound

  backup_once /etc/resolv.conf
  cat > /etc/resolv.conf <<'EOF'
# Local unbound first (needed for DNSBL lookups by Postfix); public resolvers as fallback.
nameserver 127.0.0.1
nameserver 8.8.8.8
nameserver 1.1.1.1
options timeout:2
EOF
  systemctl restart postfix   # copies resolv.conf into Postfix's chroot
}


# -------------------------------------------------------------------------------------
# 10. Postfix TLS: reuse the certificate Caddy obtained for MAIL_HOST
# -------------------------------------------------------------------------------------
step_postfix_tls() {
  log "10. Postfix TLS certificate"
  local dir=""
  for _ in $(seq 1 30); do   # wait up to ~5 min for Caddy (needs working DNS)
    dir=$(find /var/lib/caddy/.local/share/caddy/certificates -type d -name "$MAIL_HOST" 2>/dev/null | head -1 || true)
    [ -n "$dir" ] && [ -f "$dir/$MAIL_HOST.crt" ] && break
    dir=""
    sleep 10
  done
  if [ -z "$dir" ]; then
    warn "No certificate for $MAIL_HOST yet (DNS not ready?). Postfix keeps the self-signed one."
    warn "Re-run later:  sudo $0 postfix_tls"
    return 0
  fi
  postconf -e "smtpd_tls_cert_file = $dir/$MAIL_HOST.crt" "smtpd_tls_key_file = $dir/$MAIL_HOST.key"
  # Caddy renews the certificate in place; make Postfix re-read it weekly
  cat > /etc/cron.d/postfix-reload-cert <<'EOF'
# Pick up the certificate that Caddy renews for the mail host
17 4 * * 1 root /usr/sbin/postfix reload >/dev/null 2>&1
EOF
  postfix check
  systemctl reload postfix
}


# -------------------------------------------------------------------------------------
# 11. fail2ban: ban IPs that keep failing SSH / SMTP
# -------------------------------------------------------------------------------------
step_fail2ban() {
  log "11. fail2ban"
  cat > /etc/fail2ban/jail.local <<EOF
# Ban hosts that keep failing to log in. Logs come from the systemd journal.
[DEFAULT]
backend = systemd
banaction = nftables-multiport
banaction_allports = nftables-allports
# never ban: this server and the addresses from F2B_IGNORE (owner's IP, VPN subnet)
ignoreip = 127.0.0.1/8 ::1 $SERVER_IP $F2B_IGNORE
findtime = 10m
maxretry = 5
bantime = 1h
# repeat offenders are banned for longer each time (up to a week)
bantime.increment = true
bantime.maxtime = 1w

[sshd]
enabled = true
mode = aggressive

[postfix]
enabled = true
mode = more
EOF
  systemctl enable fail2ban
  systemctl restart fail2ban
}


# -------------------------------------------------------------------------------------
# 12. Automatic security updates (no automatic reboot)
# -------------------------------------------------------------------------------------
step_updates() {
  log "12. unattended-upgrades"
  cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF
  cat > /etc/apt/apt.conf.d/52unattended-upgrades-local <<'EOF'
// Security updates only (Debian's default origins); report problems to root
Unattended-Upgrade::Mail "root";
Unattended-Upgrade::MailReport "only-on-error";
Unattended-Upgrade::Remove-Unused-Dependencies "true";
Unattended-Upgrade::Automatic-Reboot "false";
EOF
  systemctl enable --now unattended-upgrades
}


# -------------------------------------------------------------------------------------
# 13. SSH: key-only login. Refuses to run without an installed key (would lock you out).
# -------------------------------------------------------------------------------------
step_ssh() {
  log "13. SSH hardening"
  if [ "$SSH_KEY_ONLY" != "yes" ]; then
    warn "SSH_KEY_ONLY is not 'yes', skipping"
    return 0
  fi
  if [ ! -s /root/.ssh/authorized_keys ]; then
    warn "/root/.ssh/authorized_keys is empty: add your public key first, then re-run: sudo $0 ssh"
    return 0
  fi
  cat > /etc/ssh/sshd_config.d/10-hardening.conf <<'EOF'
# Key-only SSH (password brute force was ~12,000 attempts a day)
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitEmptyPasswords no
PermitRootLogin prohibit-password
MaxAuthTries 3
X11Forwarding no
EOF
  sshd -t
  systemctl reload ssh
  warn "Before closing this session, check in a NEW terminal that key login works."
}


# -------------------------------------------------------------------------------------
# 14. Daily backup (database + secrets + config), kept for 14 days
# -------------------------------------------------------------------------------------
step_backup() {
  log "14. Daily backup"
  cat > /etc/systemd/system/mail-organizer-backup.service <<UNIT
[Unit]
Description=Mail Organizer backup (database, secrets, config)
After=docker.service
Requires=docker.service
# a failed run is mailed to root: unnoticed, it once failed every night for two weeks
OnFailure=mail-organizer-backup-failed.service

[Service]
Type=oneshot
ExecStart=$DEPLOY_DIR/backup.sh
UNIT
  cat > /etc/systemd/system/mail-organizer-backup-failed.service <<'UNIT'
[Unit]
Description=Mail root that the Mail Organizer backup failed

[Service]
Type=oneshot
ExecStart=/bin/sh -c '{ echo "To: root"; echo "Subject: Backup failed on %H"; echo; journalctl -u mail-organizer-backup.service -n 40 --no-pager; } | /usr/sbin/sendmail root'
UNIT
  cat > /etc/systemd/system/mail-organizer-backup.timer <<'UNIT'
[Unit]
Description=Daily Mail Organizer backup

[Timer]
OnCalendar=*-*-* 04:30
RandomizedDelaySec=15min
Persistent=true

[Install]
WantedBy=timers.target
UNIT
  systemctl daemon-reload
  systemctl enable --now mail-organizer-backup.timer
  systemctl start mail-organizer-backup.service   # first backup right away
  ls -l /var/backups/mail-organizer/latest/
}


# -------------------------------------------------------------------------------------
# 15. What to do outside the server
# -------------------------------------------------------------------------------------
dkim_value() {   # one-line TXT value from opendkim-genkey's mail.txt
  tr -d '\n\t' < "/etc/opendkim/keys/$1/mail.txt" \
    | sed -E 's/^[^(]*\(//; s/\).*$//; s/"[[:space:]]*"//g; s/"//g; s/[[:space:]]+/ /g; s/^ //; s/ $//'
}

step_summary() {
  log "15. DNS records to create at the registrar (Host = part before .$DOMAIN)"
  cat <<EOF
  A     @                      $SERVER_IP
  A     www                    $SERVER_IP
  A     $SERVICE_SUBDOMAIN                   $SERVER_IP
  A     mail                   $SERVER_IP
  MX    @                      $MAIL_HOST  (priority 10)
  MX    $SERVICE_SUBDOMAIN                   $MAIL_HOST  (priority 10)
  TXT   @                      v=spf1 mx -all
  TXT   $SERVICE_SUBDOMAIN                   v=spf1 mx -all
  TXT   _dmarc                 v=DMARC1; p=none; rua=mailto:$FORWARD_TO
  TXT   _dmarc.$SERVICE_SUBDOMAIN            v=DMARC1; p=none; rua=mailto:$FORWARD_TO
EOF
  for d in "$DOMAIN" "$SERVICE_DOMAIN"; do
    local host="mail._domainkey"
    [ "$d" = "$SERVICE_DOMAIN" ] && host="mail._domainkey.$SERVICE_SUBDOMAIN"
    [ -f "/etc/opendkim/keys/$d/mail.txt" ] && printf '  TXT   %-22s %s\n' "$host" "$(dkim_value "$d")"
  done
  cat <<EOF

  Do NOT add AAAA records unless the IPv6 address also gets a PTR (Postfix sends over IPv4 only).

  Ask the hosting provider: PTR (reverse DNS) of $SERVER_IP -> $MAIL_HOST

  Then check everything:
    BASE_URL=https://$SERVICE_DOMAIN MAIL_DOMAIN=$SERVICE_DOMAIN SERVER_IP=$SERVER_IP pytest live_tests -v
EOF
}


# -------------------------------------------------------------------------------------
ALL_STEPS=(packages app lookup_role site caddy postfix srs dkim resolver postfix_tls fail2ban updates ssh backup summary)
STEPS=("$@")
[ ${#STEPS[@]} -gt 0 ] || STEPS=("${ALL_STEPS[@]}")
for step in "${STEPS[@]}"; do
  declare -F "step_$step" >/dev/null || die "unknown step '$step' (steps: ${ALL_STEPS[*]})"
  "step_$step"
done
log "Done"
