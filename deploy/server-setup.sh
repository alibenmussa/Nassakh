#!/usr/bin/env bash
# One-time setup of a fresh Ubuntu server for Nassakh (docs/DEPLOY.md, step 3). Run it as root, from the clone:
#
#   sudo env SSH_PORT=22022 bash deploy/server-setup.sh
#
# Safe to run again: every step checks what is already there. It installs the packages, the firewall (ufw), fail2ban,
# unattended security upgrades, a swap file, Docker with the Compose plugin and /opt/nassakh. It does NOT touch
# sshd's configuration (you harden SSH by hand); it only reads it, so the firewall never shuts out the SSH port
# that is in use now.
#
# Variables (all optional):
#   SSH_PORT     the SSH port to allow in the firewall and watch with fail2ban (default 22022)
#   DEPLOY_USER  the user that runs docker compose (default ubuntu); must exist
#   APP_DIR      where the project lives (default /opt/nassakh)
#   TIMEZONE     the server's time zone, for cron and logs (default Africa/Tripoli)
#   SWAP_GB      the size of the swap file when the server has less swap than that (default 4)
#   ASSUME_YES   1: do not wait for Enter before the firewall is turned on

set -euo pipefail

SSH_PORT="${SSH_PORT:-22022}"
DEPLOY_USER="${DEPLOY_USER:-ubuntu}"
APP_DIR="${APP_DIR:-/opt/nassakh}"
TIMEZONE="${TIMEZONE:-Africa/Tripoli}"
SWAP_GB="${SWAP_GB:-4}"
ASSUME_YES="${ASSUME_YES:-0}"

export DEBIAN_FRONTEND=noninteractive

log() { printf '\n==> %s\n' "$*"; }
warn() { printf '\n!!! %s\n' "$*" >&2; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = "0" ] || die "run this as root: sudo env SSH_PORT=${SSH_PORT} bash deploy/server-setup.sh"
case "$SSH_PORT" in '' | *[!0-9]*) die "SSH_PORT must be a number, not '$SSH_PORT'" ;; esac
id "$DEPLOY_USER" > /dev/null 2>&1 || die "the user '$DEPLOY_USER' does not exist (DEPLOY_USER=...)"
[ -r /etc/os-release ] || die "this is not a Debian or Ubuntu server"
# shellcheck disable=SC1091
. /etc/os-release

# ------------------------------------------------------------------ 1. packages
log "1/9 apt update, upgrade and base packages"
apt-get update -y
apt-get -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold upgrade
apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg git ufw fail2ban python3-systemd unattended-upgrades cron jq htop tmux rsync

# ------------------------------------------------------------------ 2. time zone
log "2/9 time zone: $TIMEZONE"
if [ "$(timedatectl show -p Timezone --value 2> /dev/null || true)" != "$TIMEZONE" ]; then
    timedatectl set-timezone "$TIMEZONE" || warn "could not set the time zone to $TIMEZONE (continuing)"
fi

# ------------------------------------------------------------------ 3. swap
log "3/9 swap file (${SWAP_GB} GB)"
swap_mb=$(awk '/^SwapTotal:/ {print int($2 / 1024)}' /proc/meminfo)
if [ "${swap_mb:-0}" -ge $((SWAP_GB * 1024 - 256)) ]; then
    echo "swap is already ${swap_mb} MB: nothing to do"
else
    if [ ! -f /swapfile ]; then
        fallocate -l "${SWAP_GB}G" /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=$((SWAP_GB * 1024)) status=none
        chmod 600 /swapfile
        mkswap /swapfile > /dev/null
    fi
    swapon /swapfile 2> /dev/null || true
    grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
    echo "swap on: $(swapon --show --noheadings | tr -s ' ' | tr '\n' ';')"
fi
# use the swap only under real memory pressure
cat > /etc/sysctl.d/99-nassakh.conf << 'EOF'
vm.swappiness=10
EOF
sysctl -q -p /etc/sysctl.d/99-nassakh.conf || true

# ------------------------------------------------------------------ 4. SSH ports in use (read only)
log "4/9 SSH ports (reading sshd, not changing it)"
# Allowed in the firewall: SSH_PORT, every port sshd is configured for, every port it (or ssh.socket) listens on,
# and the port of the session you are using now, so turning the firewall on can never cut you off.
ssh_ports="$SSH_PORT"
port_of() { awk '{n = split($1, a, ":"); print a[n]}'; }
found=""
if command -v sshd > /dev/null 2>&1; then
    found="$found $(sshd -T 2> /dev/null | awk '$1 == "port" {print $2}' || true)"
fi
# listening sockets and open sessions of sshd (also sshd-session)
found="$found $(ss -H -tlnp 2> /dev/null | awk '/sshd/ {print $4}' | port_of || true)"
found="$found $(ss -H -tnp 2> /dev/null | awk '/sshd/ {print $4}' | port_of || true)"
# socket-activated sshd (Ubuntu): the port is ssh.socket's
found="$found $(systemctl show ssh.socket -p Listen --value 2> /dev/null | awk '{print $1}' | port_of || true)"
if [ -n "${SSH_CONNECTION:-}" ]; then
    found="$found $(echo "$SSH_CONNECTION" | awk '{print $4}')"
fi
for port in $found; do
    case "$port" in '' | *[!0-9]*) continue ;; esac
    case " $ssh_ports " in *" $port "*) ;; *) ssh_ports="$ssh_ports $port" ;; esac
done
echo "SSH ports kept open: $ssh_ports"

# ------------------------------------------------------------------ 5. fail2ban
log "5/9 fail2ban for sshd on: $ssh_ports"
cat > /etc/fail2ban/jail.d/nassakh-sshd.local << EOF
[sshd]
enabled = true
port = $(echo "$ssh_ports" | tr ' ' ',')
backend = systemd
maxretry = 5
findtime = 10m
bantime = 1h
EOF
systemctl enable fail2ban > /dev/null 2>&1 || true
systemctl restart fail2ban
sleep 2
fail2ban-client status sshd 2> /dev/null | sed 's/^/    /' || warn "fail2ban's sshd jail is not up: check 'systemctl status fail2ban'"

# ------------------------------------------------------------------ 6. unattended upgrades
log "6/9 unattended security upgrades"
cat > /etc/apt/apt.conf.d/20auto-upgrades << 'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
systemctl enable --now unattended-upgrades > /dev/null 2>&1 || true

# ------------------------------------------------------------------ 7. Docker
log "7/9 Docker and the Compose plugin"
if docker --version > /dev/null 2>&1 && docker compose version > /dev/null 2>&1; then
    echo "already installed: $(docker --version); $(docker compose version)"
else
    install_docker_apt() {
        local distro codename
        distro="$ID"
        [ "$distro" = "ubuntu" ] || [ "$distro" = "debian" ] || return 1
        codename="${VERSION_CODENAME:-}"
        # a brand-new release may not have its own Docker repository yet: take the newest LTS one (the packages
        # are static binaries and run on the newer release)
        if ! curl -fsI "https://download.docker.com/linux/$distro/dists/$codename/Release" > /dev/null 2>&1; then
            warn "Docker has no repository for '$codename' yet; using the one of the previous release"
            [ "$distro" = "ubuntu" ] && codename="noble" || codename="bookworm"
        fi
        install -m 0755 -d /etc/apt/keyrings
        curl -fsSL "https://download.docker.com/linux/$distro/gpg" -o /etc/apt/keyrings/docker.asc
        chmod a+r /etc/apt/keyrings/docker.asc
        echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/$distro $codename stable" \
            > /etc/apt/sources.list.d/docker.list
        apt-get update -y
        apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    }
    if ! install_docker_apt; then
        warn "the apt repository route failed; trying Docker's get.docker.com script"
        curl -fsSL https://get.docker.com | sh
    fi
    docker --version
    docker compose version || die "the Docker Compose plugin is missing after the install"
fi
systemctl enable --now docker > /dev/null 2>&1 || true
usermod -aG docker "$DEPLOY_USER"

# the daemon's own default log rotation (docker-compose.yml sets the same per service)
install -d /etc/docker
daemon_json=/etc/docker/daemon.json
[ -s "$daemon_json" ] || echo '{}' > "$daemon_json"
wanted=$(jq -S '. + {"log-driver": "json-file", "log-opts": ((."log-opts" // {}) + {"max-size": "20m", "max-file": "5"})}' "$daemon_json")
if [ "$wanted" != "$(jq -S . "$daemon_json")" ]; then
    echo "$wanted" > "$daemon_json"
    echo "docker log rotation written to $daemon_json; restarting docker"
    systemctl restart docker
else
    echo "docker log rotation already set"
fi

# ------------------------------------------------------------------ 8. the project directory
log "8/9 $APP_DIR"
install -d -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$APP_DIR"
install -d -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$APP_DIR/backups"

# ------------------------------------------------------------------ 9. firewall, last
log "9/9 firewall (ufw)"
ufw default deny incoming
ufw default allow outgoing
for port in $ssh_ports; do
    ufw allow "${port}/tcp" comment 'ssh'
done
ufw allow 80/tcp comment 'http (Caddy, certificate challenge and redirect)'
ufw allow 443/tcp comment 'https (Caddy)'
ufw allow 443/udp comment 'http/3 (Caddy)'

cat << EOF

###########################################################################
#  WARNING: the firewall is about to be turned on.
#
#  Allowed in: SSH on port(s) $ssh_ports, 80, 443. Everything else is refused.
#
#  Keep THIS session open. When it is on, open a SECOND terminal and check
#  that you can still log in:   ssh -p ${SSH_PORT} ${DEPLOY_USER}@<this server>
#  Only then close this one. If it fails, turn the firewall off from here:
#      sudo ufw disable
#
#  If your provider has its own firewall (Hostinger hPanel), it must allow
#  the same ports too.
#  Note: Docker publishes ports around ufw; that is why docker-compose.yml
#  publishes only Caddy's 80 and 443.
###########################################################################

EOF
if [ "$ASSUME_YES" != "1" ] && [ -t 0 ]; then
    read -r -p "Press Enter to turn the firewall on (Ctrl-C to stop here; nothing above is undone) " _
else
    echo "turning the firewall on in 10 seconds (Ctrl-C to stop)..."
    sleep 10
fi
ufw --force enable
ufw status verbose

log "done"
if [ -f /var/run/reboot-required ]; then
    warn "a reboot is required (kernel or libc updated): run 'sudo reboot' now, then continue with docs/DEPLOY.md"
fi
echo "Next: log out and back in as $DEPLOY_USER (so the docker group applies), then continue with step 4 of docs/DEPLOY.md."
