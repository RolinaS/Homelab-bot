# Homelab-bot

Bot Discord permettant d'envoyer des notifications sur l'état de santé de mon homelab Proxmox.

Il interroge **Prometheus** pour connaître l'état des nœuds et des conteneurs, publie des alertes dans un salon Discord, et permet de démarrer ou redémarrer certains conteneurs directement depuis Discord.

---

## Fonctionnalités

### Commandes

| Commande | Description | Accès |
|---|---|---|
| `/status` | État du homelab : CPU, RAM, température, stockage, uptime, et liste des conteneurs/VM avec leur consommation | Tout le monde |
| `/action` | Démarrer, arrêter proprement, redémarrer ou forcer l'arrêt d'un conteneur autorisé (avec confirmation pour les actions destructives) | Administrateurs du bot |
| `/aide` | Liste des commandes et des seuils d'alerte | Tout le monde |

### Notifications automatiques

- **Rapport quotidien** à heure fixe dans le salon configuré.
- **Alertes sur seuils** : CPU, RAM, stockage et température, après plusieurs vérifications consécutives pour éviter les fausses alertes, avec un rappel périodique tant que le problème persiste.
- **Retour à la normale** signalé par un message vert.
- **Arrêt d'un conteneur ou d'une VM** détecté et signalé.
- **Prometheus injoignable** : alerte si la supervision elle-même tombe.

Chaque action lancée via `/action` est journalisée dans le salon, avec l'auteur de la demande.

## Architecture

```
Discord  ◄──►  homelab-bot  ──►  Prometheus  ◄──  pve-exporter   (nœuds, conteneurs, stockage)
                    │                        ◄──  node-exporter  (température des hôtes)
                    └──────►  API Proxmox    (uniquement pour /action)
```

- Les **métriques** sont lues dans Prometheus : le bot n'interroge jamais directement les machines.
- Les **actions** passent par l'API Proxmox avec un token aux droits limités : allumer et éteindre, uniquement sur les conteneurs autorisés.

## Contenu du dépôt

```
homelab-bot/
├── bot.py              # Code du bot
├── requirements.txt    # Dépendances Python (discord.py, tzdata)
├── .env.example        # Modèle des variables de configuration
└── README.md
```

## Prérequis

- Docker, avec [Portainer](https://www.portainer.io/) (ou Docker Compose)
- Un Prometheus qui collecte :
  - [prometheus-pve-exporter](https://github.com/prometheus-pve/prometheus-pve-exporter) (job quelconque)
  - [node-exporter](https://github.com/prometheus/node_exporter) installé sur les hôtes Proxmox, dans un job nommé **`pve-host`** (pour la température)
- Un bot Discord créé sur le [Developer Portal](https://discord.com/developers/applications)

## Installation

### 1. Créer le bot Discord

1. Sur le Developer Portal : **New Application**, puis onglet **Bot** → **Reset Token** (le conserver précieusement).
2. Onglet **OAuth2 → URL Generator** :
   - Scopes : `bot` et `applications.commands`
   - Permissions : **View Channels**, **Send Messages**, **Embed Links** (ajouter **Mention Everyone** uniquement si `ALERT_MENTION` vaut `@everyone` ou `@here`)
3. Ouvrir l'URL générée et inviter le bot sur le serveur.
4. Dans Discord, activer le **mode développeur** (*Paramètres → Avancés*) pour pouvoir copier les identifiants (clic droit → *Copier l'identifiant*) du serveur, du salon et de son propre compte.

> Ne jamais cocher la permission **Administrateur** : si le token fuitait, il donnerait le contrôle total du serveur Discord.

### 2. Créer le token Proxmox (pour `/action`)

Sur un nœud Proxmox, créer un utilisateur limité au démarrage et à l'arrêt des conteneurs autorisés :

```bash
pveum user add discordbot@pve
pveum role add BotPower -privs "VM.PowerMgmt VM.Audit"
pveum aclmod /vms/<VMID> -user discordbot@pve -role BotPower
pveum user token add discordbot@pve bot --privsep 0
```

Répéter la ligne `aclmod` pour chaque conteneur à autoriser. La valeur du token n'est affichée qu'une seule fois.

### 3. Récupérer le code

Sur la machine Docker :

```bash
git clone https://github.com/RolinaS/homelab-bot.git /opt/homelab-bot
chmod 644 /opt/homelab-bot/bot.py /opt/homelab-bot/requirements.txt
```

### 4. Créer la stack

Dans Portainer : **Stacks → Add stack**.

```yaml
services:
  homelab-bot:
    image: python:3.13-slim
    container_name: homelab-bot
    restart: unless-stopped
    user: "10001:10001"
    working_dir: /app
    command: >
      sh -c "pip install -q --no-cache-dir --target /tmp/deps -r requirements.txt
      && python bot.py"
    environment:
      PYTHONPATH: /tmp/deps
      PYTHONUNBUFFERED: "1"
      HOME: /tmp
    env_file:
      - stack.env
    volumes:
      - /opt/homelab-bot:/app:ro
    networks:
      - monitoring

networks:
  monitoring:
    external: true
    name: monitoring_default   # réseau de la stack Prometheus
```

Dans **Environment variables → Advanced mode**, coller le contenu de `.env.example` rempli avec les vraies valeurs. Portainer génère lui-même le fichier `stack.env`.

Le code est monté **en lecture seule** et le bot tourne avec un **utilisateur non privilégié**. Les dépendances sont installées à chaque démarrage, ce qui prend quelques secondes.

### 5. Vérifier

Dans les logs du conteneur, attendre la ligne `Connecté en tant que …`, puis taper `/status` dans le salon. Les commandes `/` peuvent mettre une minute à apparaître ; si besoin, recharger Discord avec `Ctrl+R`.

## Configuration

Toutes les valeurs se règlent par variables d'environnement.

### Discord

| Variable | Obligatoire | Description |
|---|---|---|
| `DISCORD_TOKEN` | ✅ | Token du bot |
| `DISCORD_GUILD_ID` | ✅ | Identifiant du serveur Discord |
| `DISCORD_CHANNEL_ID` | ✅ | Identifiant du salon des notifications |
| `DISCORD_ADMIN_IDS` | ✅ | Identifiants des utilisateurs autorisés à utiliser `/action`, séparés par des virgules |
| `ALERT_MENTION` | | Mention ajoutée aux alertes : vide, `@here`, `@everyone` ou `<@ID_UTILISATEUR>` |

### Prometheus et Proxmox

| Variable | Défaut | Description |
|---|---|---|
| `PROMETHEUS_URL` | `http://prometheus:9090` | Adresse de Prometheus |
| `PVE_URL` | | Adresse de l'API Proxmox (`https://ip:8006`) |
| `PVE_NODE` | `pve` | Nom du nœud principal |
| `PVE_VERIFY_SSL` | `false` | Vérifier le certificat de l'API Proxmox |
| `PVE_TOKEN_ID` | | Identifiant du token (`discordbot@pve!bot`) |
| `PVE_TOKEN_SECRET` | | Secret du token |
| `ALLOWED_GUESTS` | | VMID autorisés pour `/action`, séparés par des virgules |

> Ne pas autoriser dans `ALLOWED_GUESTS` le conteneur qui héberge le bot lui-même : l'arrêter couperait le bot.

### Rapport et alertes

| Variable | Défaut | Description |
|---|---|---|
| `TZ` | `Europe/Paris` | Fuseau horaire |
| `REPORT_HOUR` | `9` | Heure du rapport quotidien |
| `ALERT_CPU` | `90` | Seuil CPU (%) |
| `ALERT_RAM` | `90` | Seuil RAM (%) |
| `ALERT_DISK` | `80` | Seuil de remplissage d'un stockage (%) |
| `ALERT_TEMP` | `85` | Seuil de température (°C) |
| `ALERT_CONSECUTIVE_CHECKS` | `3` | Nombre de vérifications consécutives (une par minute) avant d'alerter |
| `ALERT_COOLDOWN_MIN` | `60` | Délai avant de répéter une alerte qui persiste (minutes) |

Un indicateur passe à l'**orange** à 10 points du seuil, et au **rouge** une fois le seuil atteint.

## Mise à jour

```bash
cd /opt/homelab-bot && git pull
docker restart homelab-bot
```

Pour modifier un seuil ou un identifiant : *Portainer → Stacks → homelab-bot → Environment variables*, puis **Update the stack**.

## Sécurité

- **Aucun secret dans ce dépôt** : tokens et identifiants restent dans les variables d'environnement de Portainer. Ne jamais committer de fichier `.env`.
- Le token Proxmox ne peut qu'**allumer et éteindre** les conteneurs explicitement autorisés.
- `/action` est réservée aux utilisateurs listés dans `DISCORD_ADMIN_IDS`, et les actions destructives demandent une **confirmation**.
- Le bot n'expose **aucun port** : il se connecte lui-même à Discord.

## Dépannage

| Symptôme | Piste |
|---|---|
| Le conteneur redémarre en boucle | `docker logs homelab-bot` : variable obligatoire manquante, ou erreur `stack.env not found` (section *Environment variables* vide dans Portainer) |
| Le bot est en ligne mais les commandes `/` n'apparaissent pas | Vérifier le scope `applications.commands` dans l'URL d'invitation, puis recharger Discord |
| « Impossible de lire les métriques » | Prometheus injoignable : vérifier `PROMETHEUS_URL` et le réseau Docker `monitoring_default` |
| Température affichée `n/a` | node-exporter absent des hôtes, ou job Prometheus pas nommé `pve-host` |
| `/action` répond HTTP 401 ou 403 | Token Proxmox invalide, ou droits manquants sur le VMID (`pveum aclmod`) |
| Les encadrés ne s'affichent pas | Permission **Embed Links** manquante pour le bot |
