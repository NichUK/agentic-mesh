#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
TEMPLATE_ROOT="$REPO_ROOT/examples/projects/agentic-mesh-dev/deploy/nginx"

DOMAIN=${AGENTIC_MESH_TEAMS_INGRESS_DOMAIN:-am.nixnet.com}
LISTEN_ADDRESS=${AGENTIC_MESH_TEAMS_INGRESS_LISTEN_ADDRESS:-10.0.0.116}
MODE=${1:-stage}
SITE_PATH=/etc/nginx/sites-available/agentic-mesh-teams
SITE_LINK=/etc/nginx/sites-enabled/agentic-mesh-teams
ACME_ROOT=/var/www/agentic-mesh-acme
SECRET_ROOT=/etc/agentic-mesh
TOKEN_PATH=$SECRET_ROOT/teams-ingress-path-token

render_template() {
  template=$1
  destination=$2
  ingress_token=${3:-unused-during-http-stage}
  sed \
    -e "s/__SERVER_NAME__/$DOMAIN/g" \
    -e "s/__LISTEN_ADDRESS__/$LISTEN_ADDRESS/g" \
    -e "s/__INGRESS_TOKEN__/$ingress_token/g" \
    "$template" | sudo tee "$destination" >/dev/null
}

case "$MODE" in
  stage|activate) ;;
  *)
    echo "usage: $0 [stage|activate]" >&2
    exit 2
    ;;
esac

sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y nginx certbot
sudo install -d -m 0755 "$ACME_ROOT"
sudo rm -f /etc/nginx/sites-enabled/default

render_template "$TEMPLATE_ROOT/teams-ingress-http.conf.template" "$SITE_PATH"
sudo ln -sfn "$SITE_PATH" "$SITE_LINK"
sudo nginx -t
sudo systemctl enable --now nginx
# The package starts Nginx with its default wildcard listener. A restart is
# required when replacing that socket with the address-scoped ingress.
sudo systemctl restart nginx

if [ "$MODE" = "stage" ]; then
  echo "HTTP ACME ingress staged for $DOMAIN on $LISTEN_ADDRESS:80"
  exit 0
fi

if ! sudo test -s "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" || \
   ! sudo test -s "/etc/letsencrypt/live/$DOMAIN/privkey.pem"; then
  sudo certbot certonly \
    --webroot \
    --webroot-path "$ACME_ROOT" \
    --domain "$DOMAIN" \
    --non-interactive \
    --agree-tos \
    --register-unsafely-without-email
fi

sudo install -d -m 0700 "$SECRET_ROOT"
if [ -n "${AGENTIC_MESH_TEAMS_INGRESS_PATH_TOKEN:-}" ]; then
  printf '%s\n' "$AGENTIC_MESH_TEAMS_INGRESS_PATH_TOKEN" | \
    sudo tee "$TOKEN_PATH" >/dev/null
elif ! sudo test -s "$TOKEN_PATH"; then
  openssl rand -hex 32 | sudo tee "$TOKEN_PATH" >/dev/null
fi
sudo chmod 0600 "$TOKEN_PATH"
INGRESS_TOKEN=$(sudo cat "$TOKEN_PATH")
case "$INGRESS_TOKEN" in
  *[!A-Za-z0-9._~-]*|'')
    echo "invalid Teams ingress path token" >&2
    exit 1
    ;;
esac

render_template "$TEMPLATE_ROOT/teams-ingress.conf.template" "$SITE_PATH" "$INGRESS_TOKEN"
sudo install -d -m 0755 /etc/letsencrypt/renewal-hooks/deploy
printf '%s\n' '#!/bin/sh' 'systemctl reload nginx' | \
  sudo tee /etc/letsencrypt/renewal-hooks/deploy/reload-agentic-mesh-nginx >/dev/null
sudo chmod 0755 /etc/letsencrypt/renewal-hooks/deploy/reload-agentic-mesh-nginx
sudo nginx -t
sudo systemctl reload nginx

echo "HTTPS Teams ingress active with a capability-protected callback path"
