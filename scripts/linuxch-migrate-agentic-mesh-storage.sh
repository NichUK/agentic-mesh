#!/usr/bin/env sh
set -eu

MOUNT_POINT="${AGENTIC_MESH_STORAGE_MOUNT:-/mnt/agentic-mesh}"
SYSTEM_SOURCE="${AGENTIC_MESH_SYSTEM_SOURCE:-/home/nich/agentic-mesh-system-clean}"
PROJECTS_SOURCE="${AGENTIC_MESH_PROJECTS_SOURCE:-/home/nich/agentic-mesh-projects}"
DOCKER_ROOT="${AGENTIC_MESH_DOCKER_ROOT:-/var/snap/docker/common/var-lib-docker}"
ROOT_PARTITION="${AGENTIC_MESH_ROOT_PARTITION:-/dev/sda3}"
ROOT_LV="${AGENTIC_MESH_ROOT_LV:-/dev/mapper/ubuntu--vg-ubuntu--lv}"

run_sudo() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    else
        sudo "$@"
    fi
}

require_command() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "Missing required command: $1" >&2
        exit 1
    fi
}

detect_agentic_mesh_disk() {
    lsblk -b -dn -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT |
        awk '$3 == "disk" && $4 == "" && $5 == "" && $2 >= 450000000000 && $2 <= 600000000000 { print "/dev/" $1; exit }'
}

ensure_mount() {
    if findmnt -n "$MOUNT_POINT" >/dev/null 2>&1; then
        echo "$MOUNT_POINT is already mounted."
        return
    fi

    disk="$(detect_agentic_mesh_disk || true)"
    if [ -z "${disk:-}" ]; then
        echo "Could not find an unused 500GB-ish disk for Agentic Mesh." >&2
        echo "Attach the VHD first, then rerun this script." >&2
        exit 1
    fi

    part="${disk}1"
    echo "Preparing Agentic Mesh disk $disk at $MOUNT_POINT."
    run_sudo parted -s "$disk" mklabel gpt
    run_sudo parted -s "$disk" mkpart primary ext4 0% 100%
    run_sudo partprobe "$disk" || true
    sleep 2

    if [ ! -b "$part" ]; then
        echo "Expected partition $part was not created." >&2
        exit 1
    fi

    run_sudo mkfs.ext4 -F -L agentic-mesh "$part"
    uuid="$(blkid -s UUID -o value "$part")"
    run_sudo mkdir -p "$MOUNT_POINT"
    if ! grep -q "$uuid" /etc/fstab; then
        echo "UUID=$uuid $MOUNT_POINT ext4 defaults,nofail 0 2" | run_sudo tee -a /etc/fstab >/dev/null
    fi
    run_sudo mount "$MOUNT_POINT"
}

grow_root() {
    echo "Growing root filesystem where possible."
    if command -v growpart >/dev/null 2>&1; then
        run_sudo growpart /dev/sda 3 || true
    else
        echo "growpart not installed; skipping partition grow. Install cloud-guest-utils if root does not grow." >&2
    fi
    run_sudo pvresize "$ROOT_PARTITION" || true
    run_sudo lvextend -r -l +100%FREE "$ROOT_LV" || true
}

stop_agentic_mesh() {
    echo "Stopping Agentic Mesh compose services if present."
    if [ -d "$SYSTEM_SOURCE" ]; then
        old_pwd="$(pwd)"
        cd "$SYSTEM_SOURCE" || return
        if [ -x scripts/deploy-linuxch-compose.sh ]; then
            docker compose -f examples/projects/agentic-mesh-dev/deploy/compose/docker-compose.v4.yml -p agentic-mesh down --remove-orphans || true
        fi
        cd "$old_pwd" || return
    fi
}

stop_docker() {
    echo "Stopping Docker."
    run_sudo snap stop docker || run_sudo systemctl stop docker || true
}

start_docker() {
    echo "Starting Docker."
    run_sudo snap start docker || run_sudo systemctl start docker
}

sync_path() {
    src="$1"
    dst="$2"
    owner="$3"

    if [ ! -e "$src" ] && [ ! -L "$src" ]; then
        echo "Skipping missing path $src."
        return
    fi

    run_sudo mkdir -p "$(dirname "$dst")"
    if [ -L "$src" ]; then
        echo "$src is already a symlink."
        return
    fi

    echo "Syncing $src -> $dst."
    run_sudo mkdir -p "$dst"
    run_sudo rsync -aHAX --delete "$src"/ "$dst"/
    backup="${src}.pre-agentic-mesh-disk"
    if [ ! -e "$backup" ]; then
        run_sudo mv "$src" "$backup"
    fi
    run_sudo ln -s "$dst" "$src"
    run_sudo chown -h "$owner" "$src" || true
}

move_docker_root() {
    target="$MOUNT_POINT/docker"
    if findmnt -n "$DOCKER_ROOT" >/dev/null 2>&1; then
        echo "$DOCKER_ROOT is already a mount point."
        return
    fi

    stop_docker
    run_sudo mkdir -p "$target"
    if [ -d "$DOCKER_ROOT" ]; then
        echo "Syncing Docker root $DOCKER_ROOT -> $target."
        run_sudo rsync -aHAX --delete "$DOCKER_ROOT"/ "$target"/
        backup="${DOCKER_ROOT}.pre-agentic-mesh-disk"
        if [ ! -e "$backup" ]; then
            run_sudo mv "$DOCKER_ROOT" "$backup"
        fi
    fi
    run_sudo mkdir -p "$DOCKER_ROOT"
    if ! grep -q " $DOCKER_ROOT " /etc/fstab; then
        echo "$target $DOCKER_ROOT none bind 0 0" | run_sudo tee -a /etc/fstab >/dev/null
    fi
    run_sudo mount "$DOCKER_ROOT"
    start_docker
}

main() {
    require_command awk
    require_command blkid
    require_command docker
    require_command lsblk
    require_command parted
    require_command rsync

    ensure_mount
    grow_root
    stop_agentic_mesh

    run_sudo mkdir -p "$MOUNT_POINT/system" "$MOUNT_POINT/projects" "$MOUNT_POINT/backups"
    sync_path "$SYSTEM_SOURCE" "$MOUNT_POINT/system/system-clean" "nich:nich"
    sync_path "$PROJECTS_SOURCE" "$MOUNT_POINT/projects" "nich:nich"
    move_docker_root

    echo ""
    echo "Agentic Mesh storage migration complete."
    df -h "$MOUNT_POINT" /
    docker info --format 'DockerRootDir={{.DockerRootDir}}'
    findmnt "$DOCKER_ROOT" || true
    echo ""
    echo "Restart Agentic Mesh with:"
    echo "  cd $SYSTEM_SOURCE && AGENTIC_MESH_SYSTEM_HOST_PATH=$SYSTEM_SOURCE sh scripts/release-linuxch-compose.sh"
}

main "$@"
