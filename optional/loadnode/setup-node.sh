#!/bin/sh
# setup-node.sh - the one-time setup of a PM kit load node: a compute-only Ubuntu 24.04 box that
# the main machine reaches over SSH, as the user pmnode, with one key that can run only
# loadnode.py serve. Run it once on the box, by its owner, with sudo:
#
#   sudo sh setup-node.sh [options] <main-ip> '<main public key line>'
#
#   <main-ip>             the main machine's LAN IPv4 address (the only source the key and the
#                         firewall accept)
#   <main key line>       ssh-ed25519 <base64> [comment], exactly as main's .pub file holds it
#   --allow-user NAME     an account that keeps ssh login (repeatable): your admin account. The
#                         account you run sudo from must be one, or opted out:
#   --no-sudo-user-ssh    the sudo account gives up ssh login (console only)
#   --admin-src ADDR[/N]  another IPv4 source (or network, /8../32) that may reach port 22, such
#                         as the laptop you administer the box from (repeatable)
#   --kit-src PATH        the kit to install at /opt/pm-kit: a tar made on main with
#                         `git -C <kit> archive --format=tar -o pm-kit.tar HEAD` and copied over,
#                         or a folder holding the kit; re-run with it to update the kit
#   --dry-run             check everything and say what would change; change nothing
#
# A re-run with the same arguments changes nothing and says "unchanged"; a new key, address or
# user list replaces the old one. The box never gets GitHub access: the kit arrives only through
# --kit-src. Python 3.14 comes from the deadsnakes PPA, beside the system Python 3.12. A
# user@.service drop-in delegates the cpu, cpuset, io and memory cgroup controllers to users.

set -u
umask 022
LC_ALL=C
export LC_ALL

KIT_DIR=/opt/pm-kit
FORCED_CMD='/usr/bin/python3.14 -I -B /opt/pm-kit/optional/loadnode/loadnode.py serve'
PY=/usr/bin/python3.14
NODE_USER=pmnode
NODE_HOME=/var/lib/pmnode
AK_DIR=/etc/ssh/authorized_keys
AK_FILE=/etc/ssh/authorized_keys/pmnode
# sshd reads /etc/ssh/sshd_config.d/*.conf in name order from the Include at the top of
# Ubuntu's sshd_config, and for most keywords the first value it reads wins: this file sorts
# before cloud-init's 50-cloud-init.conf (which may say PasswordAuthentication yes).
SSHD_CONF=/etc/ssh/sshd_config.d/00-pmkit-node.conf
HOST_KEY=/etc/ssh/ssh_host_ed25519_key.pub
UFW_MAIN=pmkit-main
UFW_ADMIN=pmkit-admin
# The controllers each user's systemd manager may run in its own cgroups: pmnode's jobs pin
# their cpus (cpuset) and cap cpu, memory and io in scopes of its user manager. Ubuntu's
# user@.service delegates only cpu, memory and pids.
DELEGATE='cpu cpuset io memory pids'
DELEGATE_DIR=/etc/systemd/system/user@.service.d
DELEGATE_CONF=$DELEGATE_DIR/pmkit-delegate.conf
ED25519_PREFIX=AAAAC3NzaC1lZDI1NTE5AAAAI

say() { printf '%s\n' "$*"; }
warn() { printf 'WARNING - %s\n' "$*" >&2; }
refuse() { printf 'REFUSED - %s\n' "$*" >&2; exit 2; }
fail() { printf 'FAILED - %s\n' "$*" >&2; exit 1; }

# ---- checks (pure: no side effects; the unit test runs them) --------------------------------

# A dotted IPv4 address: four decimal parts 0-255, no leading zeros.
valid_ipv4() (
    case $1 in ''|*[!0-9.]*|.*|*.|*..*) return 1 ;; esac
    IFS=.
    set -f
    # shellcheck disable=SC2086
    set -- $1
    [ $# -eq 4 ] || return 1
    for o in "$@"; do
        case $o in 0) ;; 0*) return 1 ;; esac
        [ ${#o} -le 3 ] && [ "$o" -le 255 ] || return 1
    done
)

# An address a main machine can have: not 0.x, loopback, multicast or above.
main_ip_ok() {
    valid_ipv4 "$1" || return 1
    case $1 in 0.*|127.*) return 1 ;; esac
    [ "${1%%.*}" -lt 224 ]
}

ip_to_int() (
    IFS=.
    set -f
    # shellcheck disable=SC2086
    set -- $1
    echo $(( ($1 << 24) + ($2 << 16) + ($3 << 8) + $4 ))
)

# An IPv4 address, or network/N with N 8..31 and no host bits set. ufw stores /32 as the bare
# address and masks host bits, so those forms would never match what ufw reports back.
valid_src() {
    case $1 in
        */*) valid_ipv4 "${1%/*}" || return 1
             case ${1#*/} in ''|*[!0-9]*|0*) return 1 ;; esac
             [ "${1#*/}" -ge 8 ] && [ "${1#*/}" -le 31 ] || return 1
             [ $(( $(ip_to_int "${1%/*}") & (0xFFFFFFFF >> ${1#*/}) )) -eq 0 ] ;;
        *) valid_ipv4 "$1" ;;
    esac
}

# Is IPv4 address $1 inside source $2 (an address or address/N)?
ip_in_src() (
    case $2 in */*) net=${2%/*}; bits=${2#*/} ;; *) net=$2; bits=32 ;; esac
    a=$(ip_to_int "$1"); n=$(ip_to_int "$net")
    mask=$(( (0xFFFFFFFF << (32 - bits)) & 0xFFFFFFFF ))
    [ $(( a & mask )) -eq $(( n & mask )) ]
)

# Exactly `ssh-ed25519 <base64 of a 32-byte key> [comment]`: no options before it, no newline
# or other control character, single spaces, and the blob an ed25519 key's length and header.
valid_key_line() (
    k=$1
    [ -n "$k" ] || return 1
    nonprint=$(printf '%s' "$k" | tr -d '\040-\176' | wc -c | tr -d ' ')
    [ "$nonprint" -eq 0 ] || return 1
    case $k in "ssh-ed25519 "*) ;; *) return 1 ;; esac
    rest=${k#ssh-ed25519 }
    blob=${rest%% *}
    [ ${#blob} -eq 68 ] || return 1
    case $blob in "$ED25519_PREFIX"*) ;; *) return 1 ;; esac
    case $blob in *[!A-Za-z0-9+/]*) return 1 ;; esac
    if [ "$rest" != "$blob" ]; then
        comment=${rest#"$blob" }
        [ -n "$comment" ] || return 1
        case $comment in ' '*|*' ') return 1 ;; esac
    fi
)

# A Linux user name: lowercase letter or _ first, then lowercase letters, digits, _ and -.
valid_user() {
    case $1 in ''|[!a-z_]*|*[!a-z0-9_-]*) return 1 ;; esac
    [ ${#1} -le 32 ]
}

# Does os-release file $1 name Ubuntu 24.04?
os_release_ok() (
    id=$(sed -n 's/^ID=//p' "$1" | tr -d "\"'")
    ver=$(sed -n 's/^VERSION_ID=//p' "$1" | tr -d "\"'")
    [ "$id" = ubuntu ] && [ "$ver" = 24.04 ]
)

os_release_text() {
    sed -n 's/^PRETTY_NAME=//p' "$1" | tr -d "\"'"
}

# The one line of /etc/ssh/authorized_keys/pmnode.
ak_line() {
    printf 'from="%s",command="%s",restrict %s\n' "$1" "$FORCED_CMD" "$2"
}

# The sshd drop-in: $1 the AllowUsers list (space separated, pmnode first).
sshd_conf_text() {
    cat <<EOF
# Written by pm-kit optional/loadnode/setup-node.sh; re-run that script to change it.
# Sorted first in sshd_config.d so these values win (sshd keeps the first value it reads).
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
AllowUsers $1
Match User $NODE_USER
	AuthorizedKeysFile $AK_DIR/%u
	AuthenticationMethods publickey
	PermitTTY no
	DisableForwarding yes
EOF
}

# The user@.service drop-in that delegates $DELEGATE.
delegate_conf_text() {
    cat <<EOF
# Written by pm-kit optional/loadnode/setup-node.sh; re-run that script to change it.
[Service]
Delegate=$DELEGATE
EOF
}

# ---- arguments ------------------------------------------------------------------------------

usage() {
    sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//' >&2
    exit 2
}

DRY=0
MAIN_IP=
MAIN_KEY=
ALLOW_USERS=
ADMIN_SRCS=
KIT_SRC=
NO_SUDO_SSH=0

parse_args() {
    npos=0
    while [ $# -gt 0 ]; do
        case $1 in
            --dry-run) DRY=1 ;;
            --no-sudo-user-ssh) NO_SUDO_SSH=1 ;;
            --allow-user)
                [ $# -ge 2 ] || refuse "--allow-user needs a user name"
                valid_user "$2" || refuse "--allow-user $2 is not a valid user name"
                case $2 in root|"$NODE_USER") refuse "--allow-user $2: root and $NODE_USER are not admin accounts" ;; esac
                case " $ALLOW_USERS " in *" $2 "*) ;; *) ALLOW_USERS="${ALLOW_USERS:+$ALLOW_USERS }$2" ;; esac
                shift ;;
            --admin-src)
                [ $# -ge 2 ] || refuse "--admin-src needs an IPv4 address or address/N"
                valid_src "$2" || refuse "--admin-src $2 is not an IPv4 address or network/N (N 8..31, network address with no host bits; use the bare address for one host)"
                case " $ADMIN_SRCS " in *" $2 "*) ;; *) ADMIN_SRCS="${ADMIN_SRCS:+$ADMIN_SRCS }$2" ;; esac
                shift ;;
            --kit-src)
                [ $# -ge 2 ] && [ -n "$2" ] || refuse "--kit-src needs a path"
                KIT_SRC=$2
                shift ;;
            -h|--help) usage ;;
            -*) refuse "unknown option $1 (--help lists them)" ;;
            *)
                npos=$((npos + 1))
                case $npos in
                    1) MAIN_IP=$1 ;;
                    2) MAIN_KEY=$1 ;;
                    *) refuse "too many arguments: give <main-ip> and '<key line>' (quote the key line)" ;;
                esac ;;
        esac
        shift
    done
    [ "$npos" -eq 2 ] || refuse "give <main-ip> and '<main public key line>' (quoted, as one argument)"
    main_ip_ok "$MAIN_IP" || refuse "main IP $MAIN_IP is not a usable IPv4 address (dotted, no leading zeros)"
    valid_key_line "$MAIN_KEY" || refuse "the key line must be exactly: ssh-ed25519 <base64> [comment] - one line, no options, an ed25519 key"
    case " $ADMIN_SRCS " in *" $MAIN_IP "*) refuse "--admin-src $MAIN_IP is main's address already" ;; esac
}

# ---- (a) is this the box we expect? ----------------------------------------------------------

SUDO_ACCOUNT=

preflight() {
    [ -r /etc/os-release ] || refuse "no /etc/os-release: this is not Ubuntu 24.04"
    os_release_ok /etc/os-release || refuse "this is $(os_release_text /etc/os-release), not Ubuntu 24.04 LTS"
    [ -d /run/systemd/system ] && [ "$(cat /proc/1/comm 2>/dev/null)" = systemd ] \
        || refuse "systemd is not PID 1 here (PID 1 is $(cat /proc/1/comm 2>/dev/null || echo unknown))"
    [ "$(id -u)" -eq 0 ] || refuse "run it as root with sudo: sudo sh $0 ..."
    SUDO_ACCOUNT=${SUDO_USER:-}
    [ -n "$SUDO_ACCOUNT" ] && [ "$SUDO_ACCOUNT" != root ] \
        || refuse "run it with sudo from your admin account (SUDO_USER is ${SUDO_ACCOUNT:-unset}), not as root directly"

    # AllowUsers will name only pmnode and --allow-user accounts: the sudo account must be one
    # of them, or opted out, so the owner cannot lock themselves out by forgetting it.
    case " $ALLOW_USERS " in
        *" $SUDO_ACCOUNT "*) [ "$NO_SUDO_SSH" -eq 0 ] \
            || refuse "--no-sudo-user-ssh and --allow-user $SUDO_ACCOUNT contradict each other" ;;
        *) [ "$NO_SUDO_SSH" -eq 1 ] \
            || refuse "AllowUsers would lock out $SUDO_ACCOUNT, the account you ran sudo from: add --allow-user $SUDO_ACCOUNT, or --no-sudo-user-ssh to give up its ssh login" ;;
    esac
    for u in $ALLOW_USERS; do
        getent passwd "$u" >/dev/null || refuse "--allow-user $u: no such account on this box"
        # Password login goes off: each kept account needs a key to log in with.
        home=$(getent passwd "$u" | cut -d: -f6)
        if ! grep -Eqs '(^|[ ,"])(ssh-(ed25519|rsa|dss)|ecdsa-sha2-nistp[0-9]+|sk-[a-z0-9@.-]+) AAAA' \
                "$home/.ssh/authorized_keys" "$home/.ssh/authorized_keys2"; then
            refuse "$u has no key in $home/.ssh/authorized_keys: with password login off it could not log in by ssh. Add a key first (ssh-copy-id from your computer)"
        fi
    done

    # The firewall will let port 22 in only from main and --admin-src: the ssh session this runs
    # in (if any) must be one of them, or the next login from here is refused.
    from=$(who -m 2>/dev/null | sed -n 's/.*(\([^)]*\)).*/\1/p')
    if [ -n "$from" ]; then
        if valid_ipv4 "$from"; then
            ok=0
            for s in "$MAIN_IP" $ADMIN_SRCS; do ip_in_src "$from" "$s" && ok=1; done
            if [ "$ok" -eq 0 ]; then
                if [ "$NO_SUDO_SSH" -eq 1 ]; then
                    warn "this session comes from $from, which the firewall will not let reach port 22 again"
                else
                    refuse "this session comes from $from, which the firewall would shut out: add --admin-src $from (or its network, e.g. --admin-src ${from%.*}.0/24)"
                fi
            fi
        else
            warn "this session comes from $from, not an IPv4 address: after the firewall is on, only $MAIN_IP${ADMIN_SRCS:+ and $ADMIN_SRCS} reach port 22 (IPv4)"
        fi
    fi

    if [ -x /usr/sbin/sshd ] && [ -d /run/sshd ]; then     # sshd -T needs /run/sshd
        ports=$(/usr/sbin/sshd -T 2>/dev/null | sed -n 's/^port //p' | tr '\n' ' ')
        case $ports in ''|'22 ') ;; *) refuse "sshd listens on port(s) $ports, not only 22: this script opens only 22 in the firewall" ;; esac
    fi
    if grep -rlsw "$NODE_USER" /etc/sudoers /etc/sudoers.d >/dev/null 2>&1; then
        refuse "sudoers names $NODE_USER ($(grep -rlsw "$NODE_USER" /etc/sudoers /etc/sudoers.d | tr '\n' ' ')): it must have no sudo"
    fi
    if [ -n "$KIT_SRC" ] && [ ! -e "$KIT_SRC" ]; then
        refuse "--kit-src $KIT_SRC does not exist"
    fi
    [ ! -L "$AK_DIR" ] && [ ! -L "$AK_FILE" ] && [ ! -L "$SSHD_CONF" ] && [ ! -L "$KIT_DIR" ] \
        || refuse "one of $AK_DIR, $AK_FILE, $SSHD_CONF, $KIT_DIR is a symlink; remove it first"
    say "checked: $(os_release_text /etc/os-release), systemd, root via sudo from $SUDO_ACCOUNT, main $MAIN_IP"
}

# ---- bookkeeping ----------------------------------------------------------------------------

CHANGED=
TMP=

mark() {   # $1 step letter, $2 what changed (or would)
    case " $CHANGED " in *" $1 "*) ;; *) CHANGED="${CHANGED:+$CHANGED }$1" ;; esac
    if [ "$DRY" -eq 1 ]; then say "  [$1] would change: $2"; else say "  [$1] changed: $2"; fi
}

same() { say "  [$1] unchanged: $2"; }

run() {    # a command that changes the box; printed, not run, on a dry run
    if [ "$DRY" -eq 1 ]; then say "      would run: $*"; return 0; fi
    say "      run: $*"
    "$@"
}

owner_mode() { stat -c '%U:%G %a' "$1" 2>/dev/null; }

# Install $1 at $2 (root:root, mode $3) unless it is already exactly that. 0 = changed.
put_file() {
    if [ -f "$2" ] && cmp -s "$1" "$2" && [ "$(owner_mode "$2")" = "root:root $3" ]; then return 1; fi
    [ "$DRY" -eq 1 ] && return 0
    new="$(dirname "$2")/.pmkit-new.$$"
    cp "$1" "$new" && chown root:root "$new" && chmod "$3" "$new" && mv -f "$new" "$2" \
        || fail "could not write $2"
    return 0
}

# ---- (b) Python 3.14, git, openssh-server, ufw ----------------------------------------------

pkg_installed() { dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q 'install ok installed'; }

step_packages() {
    need=
    pkg_installed git || need="$need git"
    pkg_installed openssh-server || need="$need openssh-server"
    pkg_installed ufw || need="$need ufw"
    want_py=0
    [ -x "$PY" ] || want_py=1
    if [ -z "$need" ] && [ "$want_py" -eq 0 ]; then
        same b "python3.14 ($($PY -c 'import sys; print(sys.version.split()[0])' 2>/dev/null)), git, openssh-server, ufw present"
        return
    fi
    mark b "install${need}$( [ "$want_py" -eq 1 ] && printf ' python3.14 (deadsnakes PPA)')"
    if [ "$want_py" -eq 1 ]; then
        if ! ls /etc/apt/sources.list.d/deadsnakes-ubuntu-ppa-*.sources /etc/apt/sources.list.d/deadsnakes-ubuntu-ppa-*.list >/dev/null 2>&1; then
            command -v add-apt-repository >/dev/null 2>&1 || {
                run apt-get update || fail "apt-get update failed"
                run env DEBIAN_FRONTEND=noninteractive apt-get install -y software-properties-common \
                    || fail "could not install software-properties-common"
            }
            run add-apt-repository -y ppa:deadsnakes/ppa || fail "could not add the deadsnakes PPA"
        fi
    fi
    run apt-get update || fail "apt-get update failed"
    if [ "$want_py" -eq 1 ] && [ "$DRY" -eq 0 ]; then
        cand=$(apt-cache policy python3.14 | sed -n 's/^ *Candidate: //p')
        case $cand in ''|'(none)') fail "apt offers no python3.14 (deadsnakes for noble): $(apt-cache policy python3.14 | tr '\n' ' ')" ;; esac
        need="$need python3.14"
    elif [ "$want_py" -eq 1 ]; then
        need="$need python3.14"
    fi
    # shellcheck disable=SC2086
    run env DEBIAN_FRONTEND=noninteractive apt-get install -y $need || fail "apt-get install$need failed"
    if [ "$DRY" -eq 0 ]; then
        "$PY" -I -c 'import sys; assert sys.version_info[:2] == (3, 14), sys.version' \
            || fail "$PY is not Python 3.14 after the install"
    fi
}

# ---- (c) the user pmnode: no sudo, no password ----------------------------------------------

step_user() {
    if ! getent passwd "$NODE_USER" >/dev/null; then
        mark c "create system user $NODE_USER (home $NODE_HOME, shell /bin/sh, no password)"
        run useradd --system --user-group --create-home --home-dir "$NODE_HOME" --shell /bin/sh "$NODE_USER" \
            || fail "useradd $NODE_USER failed"
        run usermod -p '*' "$NODE_USER" || fail "could not clear $NODE_USER's password"
        run chmod 0750 "$NODE_HOME"
        return
    fi
    what=
    # The forced command runs through the login shell, so it must be a real shell.
    [ "$(getent passwd "$NODE_USER" | cut -d: -f7)" = /bin/sh ] || {
        what="$what shell"; run usermod -s /bin/sh "$NODE_USER"; }
    # '*' is no password at all (no login by password, and not a "locked" account sshd refuses
    # when UsePAM is off).
    [ "$(getent shadow "$NODE_USER" | cut -d: -f2)" = '*' ] || {
        what="$what password"; run usermod -p '*' "$NODE_USER"; }
    for g in sudo admin wheel; do
        if id -nG "$NODE_USER" | tr ' ' '\n' | grep -qx "$g"; then
            what="$what group:$g"; run gpasswd -d "$NODE_USER" "$g"
        fi
    done
    nh=$(getent passwd "$NODE_USER" | cut -d: -f6)
    if [ ! -d "$nh" ]; then
        what="$what home"
        run install -d -m 0750 -o "$NODE_USER" -g "$(id -gn "$NODE_USER")" "$nh"
    fi
    if [ -n "$what" ]; then mark c "$NODE_USER:$what"; else same c "user $NODE_USER (no password, no sudo, shell /bin/sh)"; fi
}

# ---- (d) /opt/pm-kit: root-owned, read-only to pmnode ---------------------------------------

# Files or folders under $1 that are not root:root, are group/other-writable, or that others
# cannot read (folders: enter).
bad_perms() {
    find "$1" \( ! -user root -o ! -group root -o -perm /022 \
        -o \( -type f ! -perm -004 \) -o \( -type d ! -perm -005 \) \) -print 2>/dev/null | head -n 5
}

manifest() { (cd "$1" && find . -printf '%y %m %p\n' | sort); }

step_kit() {
    if [ -n "$KIT_SRC" ]; then
        stage="$TMP/kit"
        mkdir -p "$stage"
        if [ -d "$KIT_SRC" ]; then
            (cd "$KIT_SRC" && tar --exclude=./.git -cf - .) | tar -xf - -C "$stage" --no-same-owner \
                || fail "could not copy $KIT_SRC"
        else
            tar -xf "$KIT_SRC" -C "$stage" --no-same-owner || fail "could not unpack $KIT_SRC (a tar from git archive)"
        fi
        [ -f "$stage/kit.json" ] || refuse "--kit-src $KIT_SRC is not the kit: no kit.json at its top (git archive with no --prefix)"
        odd=$(find "$stage" ! -type f ! -type d | head -n 3)
        [ -z "$odd" ] || refuse "--kit-src holds links or special files (${odd#"$stage"/}...): give a plain git archive"
        chown -R root:root "$stage"
        chmod -R u+rwX,go+rX,go-w "$stage"
        if [ -d "$KIT_DIR" ] && [ "$(manifest "$stage")" = "$(manifest "$KIT_DIR")" ] \
                && diff -r -q "$stage" "$KIT_DIR" >/dev/null 2>&1; then
            same d "$KIT_DIR matches $KIT_SRC"
        else
            mark d "install $KIT_SRC at $KIT_DIR"
            if [ "$DRY" -eq 0 ]; then
                rm -rf /opt/.pm-kit.new /opt/.pm-kit.old
                mkdir -p /opt
                mv "$stage" /opt/.pm-kit.new || fail "could not stage the kit in /opt"
                chmod 0755 /opt/.pm-kit.new
                if [ -d "$KIT_DIR" ]; then mv "$KIT_DIR" /opt/.pm-kit.old || fail "could not move the previous install aside"; fi
                mv /opt/.pm-kit.new "$KIT_DIR" || fail "could not put the kit in place"
                rm -rf /opt/.pm-kit.old
            fi
        fi
    elif [ ! -d "$KIT_DIR" ]; then
        mark d "create $KIT_DIR (empty: re-run with --kit-src to put the kit there)"
        run install -d -m 0755 -o root -g root "$KIT_DIR"
    else
        same d "$KIT_DIR present (no --kit-src: its contents are left as they are)"
    fi
    if [ -d "$KIT_DIR" ]; then
        bad=$(bad_perms "$KIT_DIR")
        if [ -n "$bad" ]; then
            mark d "make $KIT_DIR root-owned and read-only to others (e.g. $(printf '%s' "$bad" | head -n 1))"
            run chown -R root:root "$KIT_DIR"
            run chmod -R u+rwX,go+rX,go-w "$KIT_DIR"
        fi
        [ -f "$KIT_DIR/optional/loadnode/loadnode.py" ] \
            || warn "$KIT_DIR/optional/loadnode/loadnode.py is missing: main's key reaches nothing until the kit is installed (--kit-src)"
    fi
}

# ---- (e) the root-owned authorized_keys for pmnode ------------------------------------------

step_authkeys() {
    if [ -d "$AK_DIR" ] && [ "$(owner_mode "$AK_DIR")" = "root:root 755" ]; then :; else
        mark e "folder $AK_DIR (root, 0755)"
        run install -d -m 0755 -o root -g root "$AK_DIR"
    fi
    ak_line "$MAIN_IP" "$MAIN_KEY" > "$TMP/ak"
    if put_file "$TMP/ak" "$AK_FILE" 644; then
        mark e "$AK_FILE: main's key, from=$MAIN_IP, forced to loadnode.py serve"
    else
        same e "$AK_FILE"
    fi
}

# ---- (f) the sshd drop-in -------------------------------------------------------------------

# The effective config agrees with the drop-in, for pmnode from main and each kept account.
sshd_effective_ok() {
    t=$(/usr/sbin/sshd -T -C "user=$NODE_USER,host=pmkit-main,addr=$MAIN_IP" 2>&1) || {
        say "      sshd -T: $t"; return 1; }
    for want in "passwordauthentication no" "kbdinteractiveauthentication no" "permitrootlogin no" \
            "pubkeyauthentication yes" "authorizedkeysfile $AK_DIR/%u" "permittty no" \
            "allowusers $NODE_USER"; do
        printf '%s\n' "$t" | grep -qxi "$want" || {
            say "      effective config for $NODE_USER lacks '$want' (another file in /etc/ssh/sshd_config.d sorts first, or sshd_config sets it): $(printf '%s\n' "$t" | grep -i "^${want%% *} " | tr '\n' ' ')"
            return 1; }
    done
    for u in $ALLOW_USERS; do
        t=$(/usr/sbin/sshd -T -C "user=$u,host=admin,addr=$MAIN_IP" 2>&1) || { say "      sshd -T: $t"; return 1; }
        for want in "passwordauthentication no" "allowusers $u"; do
            printf '%s\n' "$t" | grep -qxi "$want" || { say "      effective config for $u lacks '$want'"; return 1; }
        done
    done
    extra=$(printf '%s\n' "$t" | sed -n 's/^allowusers //p' | while read -r x; do
        case " $NODE_USER $ALLOW_USERS " in *" $x "*) ;; *) printf '%s ' "$x" ;; esac; done)
    [ -z "$extra" ] || warn "another sshd file also allows: $extra"
    return 0
}

step_sshd() {
    allow="$NODE_USER${ALLOW_USERS:+ $ALLOW_USERS}"
    sshd_conf_text "$allow" > "$TMP/sshd.conf"
    if [ -f "$SSHD_CONF" ] && cmp -s "$TMP/sshd.conf" "$SSHD_CONF" \
            && [ "$(owner_mode "$SSHD_CONF")" = "root:root 644" ]; then
        same f "$SSHD_CONF (AllowUsers $allow)"
        if [ "$DRY" -eq 0 ]; then
            install -d -m 0755 /run/sshd
            /usr/sbin/sshd -t || fail "sshd -t fails with the current config: fix it before re-running"
            sshd_effective_ok || fail "the effective sshd config does not match $SSHD_CONF"
        fi
        return
    fi
    mark f "$SSHD_CONF: passwords and root login off, AllowUsers $allow, pmnode's keys only from $AK_FILE"
    [ "$DRY" -eq 1 ] && return
    had=0
    if [ -f "$SSHD_CONF" ]; then cp -p "$SSHD_CONF" "$TMP/sshd.prev"; had=1; fi
    put_file "$TMP/sshd.conf" "$SSHD_CONF" 644
    install -d -m 0755 /run/sshd
    if /usr/sbin/sshd -t && sshd_effective_ok; then
        say "      sshd -t: OK"
        if systemctl is-active --quiet ssh.service; then
            run systemctl reload ssh.service || fail "systemctl reload ssh failed (the config passed sshd -t)"
        else
            say "      ssh.service is not running (socket activation): the next connection starts it with this config"
        fi
    else
        if [ "$had" -eq 1 ]; then cp -p "$TMP/sshd.prev" "$SSHD_CONF"; else rm -f "$SSHD_CONF"; fi
        fail "the new sshd config did not pass (above); $SSHD_CONF restored to what it was, ssh not reloaded"
    fi
}

# ---- (g) ufw: port 22 from main (and --admin-src) only --------------------------------------

# Our rules in ufw's own record (`ufw show added`), as src|tag words: the rules this script
# added before, whatever address they hold now.
ufw_ours() {
    ufw show added 2>/dev/null \
        | sed -n "s/^ufw allow from \([0-9./]*\) to any port 22 proto tcp comment '\($UFW_MAIN\|$UFW_ADMIN\)'\$/\1|\2/p"
}

step_ufw() {
    if [ "$DRY" -eq 1 ] && ! command -v ufw >/dev/null 2>&1; then
        mark g "allow 22/tcp from $MAIN_IP${ADMIN_SRCS:+ and $ADMIN_SRCS}, default deny incoming, enable ufw"
        return
    fi
    want="$MAIN_IP|$UFW_MAIN"
    for s in $ADMIN_SRCS; do want="$want $s|$UFW_ADMIN"; done
    have=$(ufw_ours | tr '\n' ' ')
    did=
    # New rules are added before any stale one is deleted, so port 22 from main is not shut by a
    # plain change. The one exception is a retag: ufw's rule identity leaves out the comment, so
    # an address that moves between main and admin cannot be added again until its old rule is
    # deleted (ufw would skip it as existing), and that rule is deleted just before it is re-added.
    for r in $want; do
        case " $have " in *" $r "*) continue ;; esac
        case " $have " in
            *" ${r%|*}|"*)
                did="$did retag:${r%|*}"
                run ufw delete allow from "${r%|*}" to any port 22 proto tcp >/dev/null \
                    || fail "ufw delete of the rule from ${r%|*} failed" ;;
            *) did="$did allow:${r%|*}" ;;
        esac
        run ufw allow from "${r%|*}" to any port 22 proto tcp comment "${r#*|}" >/dev/null \
            || fail "ufw allow from ${r%|*} failed"
    done
    for r in $have; do
        case " $want " in *" ${r%|*}|"*) continue ;; esac
        did="$did delete:${r%|*}"
        run ufw delete allow from "${r%|*}" to any port 22 proto tcp >/dev/null \
            || fail "ufw delete of the old rule from ${r%|*} failed"
    done
    if ! grep -qx 'DEFAULT_INPUT_POLICY="DROP"' /etc/default/ufw 2>/dev/null; then
        did="$did default-deny"
        run ufw default deny incoming >/dev/null || fail "ufw default deny incoming failed"
    fi
    if ! ufw status 2>/dev/null | grep -qx 'Status: active'; then
        did="$did enable"
        run ufw --force enable >/dev/null || fail "ufw enable failed"
    fi
    if [ -n "$did" ]; then
        mark g "ufw:$did (22/tcp from $MAIN_IP${ADMIN_SRCS:+ and $ADMIN_SRCS} only, default deny incoming)"
    else
        same g "ufw active, default deny incoming, 22/tcp from $MAIN_IP${ADMIN_SRCS:+ and $ADMIN_SRCS}"
    fi
    other=$(ufw show added 2>/dev/null | sed -n 's/^ufw //p' | grep -E '^allow' \
        | grep -vE "comment '($UFW_MAIN|$UFW_ADMIN)'\$" | tr '\n' ';')
    [ -z "$other" ] || warn "other ufw allow rules remain (delete any that open port 22 wider than main): $other"
}

# ---- (h) cgroup delegation to pmnode's systemd user manager ---------------------------------

# systemd's view of pmnode's user manager names every controller in $DELEGATE (read from the
# loaded unit, so a drop-in written without a daemon-reload shows as missing).
delegate_loaded() (
    uid=$(id -u "$NODE_USER" 2>/dev/null) || return 1
    have=$(systemctl show -p DelegateControllers --value "user@$uid.service" 2>/dev/null)
    for c in $DELEGATE; do case " $have " in *" $c "*) ;; *) return 1 ;; esac; done
)

step_delegate() {
    delegate_conf_text > "$TMP/delegate.conf"
    if [ ! -d "$DELEGATE_DIR" ]; then
        mark h "folder $DELEGATE_DIR (root, 0755)"
        run install -d -m 0755 -o root -g root "$DELEGATE_DIR" || fail "could not make $DELEGATE_DIR"
    fi
    if put_file "$TMP/delegate.conf" "$DELEGATE_CONF" 644; then
        mark h "$DELEGATE_CONF: Delegate=$DELEGATE for each user's systemd manager (applies from its next start)"
    elif [ "$DRY" -eq 0 ] && ! delegate_loaded; then
        mark h "systemd had not loaded $DELEGATE_CONF"
    else
        same h "$DELEGATE_CONF (Delegate=$DELEGATE)"
        return
    fi
    run systemctl daemon-reload || fail "systemctl daemon-reload failed"
    if [ "$DRY" -eq 0 ]; then
        delegate_loaded || fail "after daemon-reload, user@$(id -u "$NODE_USER").service still does not delegate $DELEGATE: $(systemctl show -p DelegateControllers "user@$(id -u "$NODE_USER").service" 2>&1)"
    fi
}

# ---- (i) what to paste on main --------------------------------------------------------------

step_report() {
    say ""
    if [ ! -f "$HOST_KEY" ]; then
        say "host key: $HOST_KEY not there yet (openssh-server is installed on a real run)"
        return
    fi
    hk=$(cut -d' ' -f1,2 "$HOST_KEY")
    say "host key:    $hk"
    say "fingerprint: $(ssh-keygen -lf "$HOST_KEY")"
    addr=$(ip -4 route get "$MAIN_IP" 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p')
    name=$(hostname | tr 'A-Z' 'a-z' | tr -c 'a-z0-9-\n' '-' | cut -c1-32)
    threads=$(nproc 2>/dev/null || echo 1)
    slots=$(( threads / 8 )); [ "$slots" -ge 1 ] || slots=1
    mem=$(awk '/^MemTotal:/ { printf "%d", $2 * 3 / 4 / 1024 }' /proc/meminfo)
    say "registry entry for main (<PM repo>/.pm-kit/load-nodes.json, \"nodes\" list; slots and memory_mb are suggestions):"
    say "  {\"name\": \"$name\", \"address\": \"${addr:-<this box's LAN IP>}\", \"user\": \"$NODE_USER\", \"host_key\": \"$hk\", \"slots\": $slots, \"memory_mb\": $mem, \"identity_file\": \"~/.ssh/pmkit_node_ed25519\"}"
}

main() {
    parse_args "$@"
    preflight
    TMP=$(mktemp -d /tmp/pmkit-setup.XXXXXX) || fail "mktemp failed"
    trap 'rm -rf "$TMP"' EXIT
    trap 'exit 1' INT TERM HUP
    [ "$DRY" -eq 1 ] && say "dry run: nothing on this box will be changed"
    step_packages
    step_user
    step_kit
    step_authkeys
    step_sshd
    step_ufw
    step_delegate
    step_report
    say ""
    if [ -z "$CHANGED" ]; then
        say "RESULT: unchanged"
    elif [ "$DRY" -eq 1 ]; then
        say "RESULT: dry run, would change [$CHANGED]"
    else
        say "RESULT: changed [$CHANGED]"
    fi
}

# Sourced with PMKIT_SETUP_NODE_LIB=1 (the unit test), it only defines the functions.
[ "${PMKIT_SETUP_NODE_LIB:-}" = 1 ] || main "$@"
