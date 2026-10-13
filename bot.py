"""
Bot Discord de supervision du homelab Proxmox.

Commandes :
  /status  : état de l'hôte (CPU, RAM, température, stockage, uptime) et des invités
  /action  : démarrer / arrêter / redémarrer un invité autorisé (admins, avec confirmation)
  /aide    : liste des commandes et des seuils d'alerte

Automatique :
  - rapport quotidien dans le salon configuré
  - alertes sur seuils (CPU, RAM, température, stockage) et sur l'arrêt d'un invité
  - alerte si Prometheus devient injoignable

Les métriques viennent de Prometheus (pve-exporter + node-exporter de l'hôte).
Les actions passent par l'API Proxmox avec un token aux droits limités.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("homelab-bot")


# --------------------------------------------------------------------------- configuration
def _env_list(name: str) -> list[str]:
    return [x.strip() for x in os.getenv(name, "").split(",") if x.strip()]


def _env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "oui"}


DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
GUILD = discord.Object(id=int(os.environ["DISCORD_GUILD_ID"]))
CHANNEL_ID = int(os.environ["DISCORD_CHANNEL_ID"])
ADMIN_IDS = {int(x) for x in _env_list("DISCORD_ADMIN_IDS")}
ALERT_MENTION = os.getenv("ALERT_MENTION", "").strip()

PROM_URL = os.getenv("PROMETHEUS_URL", "http://prometheus:9090").rstrip("/")
PVE_URL = os.getenv("PVE_URL", "").rstrip("/")
PVE_NODE = os.getenv("PVE_NODE", "pve")
PVE_VERIFY_SSL = _env_bool("PVE_VERIFY_SSL", False)
PVE_TOKEN_ID = os.getenv("PVE_TOKEN_ID", "")
PVE_TOKEN_SECRET = os.getenv("PVE_TOKEN_SECRET", "")
ALLOWED_GUESTS = {int(x) for x in _env_list("ALLOWED_GUESTS")}

TZ = ZoneInfo(os.getenv("TZ", "Europe/Paris"))
REPORT_HOUR = int(os.getenv("REPORT_HOUR", "9"))

THRESHOLDS = {
    "cpu": float(os.getenv("ALERT_CPU", "90")),
    "ram": float(os.getenv("ALERT_RAM", "90")),
    "disk": float(os.getenv("ALERT_DISK", "80")),
    "temp": float(os.getenv("ALERT_TEMP", "85")),
}
WARN_MARGIN = 10  # à moins de 10 points du seuil : affichage en orange
ALERT_CHECKS = max(1, int(os.getenv("ALERT_CONSECUTIVE_CHECKS", "3")))
ALERT_COOLDOWN = int(os.getenv("ALERT_COOLDOWN_MIN", "60")) * 60
CHECK_INTERVAL = 60  # secondes entre deux vérifications

GREEN, ORANGE, RED, GREY = 0x2ECC71, 0xF39C12, 0xE74C3C, 0x95A5A6
ICONS = {"green": "✅", "orange": "⚠️", "red": "🔴", "grey": "❔"}

ACTIONS = {
    "start": "Démarrer",
    "shutdown": "Arrêt propre",
    "reboot": "Redémarrer",
    "stop": "Arrêt forcé",
}

NODE_SEL = f'id="node/{PVE_NODE}"'
GUEST_SEL = 'id=~"(lxc|qemu)/.*"'
QUERIES = {
    "cpu": f"pve_cpu_usage_ratio{{{NODE_SEL}}} * 100",
    "ram": f"pve_memory_usage_bytes{{{NODE_SEL}}} / pve_memory_size_bytes{{{NODE_SEL}}} * 100",
    "uptime": f"pve_uptime_seconds{{{NODE_SEL}}}",
    "temp": 'max(node_hwmon_temp_celsius{job="pve-host"})',
    "storage": 'pve_disk_usage_bytes{id=~"storage/.*"} / pve_disk_size_bytes{id=~"storage/.*"} * 100',
    "guest_info": "pve_guest_info",
    "guest_up": f"pve_up{{{GUEST_SEL}}}",
    "guest_cpu": f"pve_cpu_usage_ratio{{{GUEST_SEL}}} * 100",
    "guest_ram": f"pve_memory_usage_bytes{{{GUEST_SEL}}} / pve_memory_size_bytes{{{GUEST_SEL}}} * 100",
}


# --------------------------------------------------------------------------- collecte des métriques
class Prometheus:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    async def query(self, promql: str) -> list[tuple[dict, float]]:
        async with self.session.get(f"{PROM_URL}/api/v1/query", params={"query": promql}) as resp:
            resp.raise_for_status()
            payload = await resp.json()
        if payload.get("status") != "success":
            raise RuntimeError(f"Requête Prometheus refusée : {promql}")
        results = []
        for item in payload["data"]["result"]:
            try:
                value = float(item["value"][1])
            except (KeyError, IndexError, ValueError):
                continue
            if math.isfinite(value):
                results.append((item["metric"], value))
        return results


async def collect(prom: Prometheus) -> dict:
    """Interroge Prometheus et renvoie un instantané de l'état du homelab."""
    keys = list(QUERIES)
    raw = await asyncio.gather(*(prom.query(QUERIES[k]) for k in keys))
    r = dict(zip(keys, raw))

    def first(key: str):
        return r[key][0][1] if r[key] else None

    def by_id(key: str) -> dict[str, float]:
        return {m.get("id", ""): v for m, v in r[key]}

    names = {m.get("id", ""): m.get("name") or m.get("id", "?") for m, _ in r["guest_info"]}
    up, gcpu, gram = by_id("guest_up"), by_id("guest_cpu"), by_id("guest_ram")

    guests = []
    for gid, state in up.items():
        gtype, _, vmid = gid.partition("/")
        if not vmid.isdigit():
            continue
        guests.append({
            "id": gid,
            "type": gtype,
            "vmid": int(vmid),
            "name": names.get(gid, gid),
            "running": state >= 1,
            "cpu": gcpu.get(gid),
            "ram": gram.get(gid),
        })
    guests.sort(key=lambda g: g["vmid"])

    storage = {m.get("id", "").rsplit("/", 1)[-1]: v for m, v in r["storage"]}

    return {
        "cpu": first("cpu"),
        "ram": first("ram"),
        "uptime": first("uptime"),
        "temp": first("temp"),
        "storage": dict(sorted(storage.items())),
        "guests": guests,
    }


# --------------------------------------------------------------------------- mise en forme
def level(value, kind: str) -> str:
    if value is None:
        return "grey"
    crit = THRESHOLDS[kind]
    if value >= crit:
        return "red"
    if value >= crit - WARN_MARGIN:
        return "orange"
    return "green"


def bar(value, width: int = 12) -> str:
    if value is None:
        return "░" * width
    filled = round(min(max(value, 0), 100) / 100 * width)
    return "█" * filled + "░" * (width - filled)


def fmt_pct(value) -> str:
    return "n/a" if value is None else f"{value:.0f} %"


def fmt_uptime(seconds) -> str:
    if seconds is None:
        return "n/a"
    s = int(seconds)
    days, s = divmod(s, 86400)
    hours, s = divmod(s, 3600)
    minutes = s // 60
    return f"{days} j {hours} h {minutes} min" if days else f"{hours} h {minutes} min"


def worst_color(levels: list[str]) -> int:
    if "red" in levels:
        return RED
    if "orange" in levels:
        return ORANGE
    if levels and all(lv == "grey" for lv in levels):
        return GREY
    return GREEN


def guests_text(guests: list[dict]) -> str:
    lines = []
    for g in guests:
        kind = "CT" if g["type"] == "lxc" else "VM"
        if g["running"]:
            lines.append(
                f"🟢 **{g['vmid']} {g['name']}** ({kind}) — CPU {fmt_pct(g['cpu'])} · RAM {fmt_pct(g['ram'])}"
            )
        else:
            lines.append(f"🔴 **{g['vmid']} {g['name']}** ({kind}) — arrêté")
    return "\n".join(lines)[:1024]


def status_embed(s: dict, title: str = "🖥️ État du homelab") -> discord.Embed:
    lv_cpu, lv_ram, lv_temp = level(s["cpu"], "cpu"), level(s["ram"], "ram"), level(s["temp"], "temp")
    disk_levels = {name: level(v, "disk") for name, v in s["storage"].items()}

    embed = discord.Embed(
        title=title,
        color=worst_color([lv_cpu, lv_ram, lv_temp, *disk_levels.values()]),
        timestamp=datetime.now(TZ),
    )
    embed.add_field(name="⚙️ CPU", value=f"{ICONS[lv_cpu]} **{fmt_pct(s['cpu'])}**\n`{bar(s['cpu'])}`")
    embed.add_field(name="🧠 RAM", value=f"{ICONS[lv_ram]} **{fmt_pct(s['ram'])}**\n`{bar(s['ram'])}`")
    temp_txt = "n/a" if s["temp"] is None else f"{s['temp']:.0f} °C"
    embed.add_field(name="🌡️ Température", value=f"{ICONS[lv_temp]} **{temp_txt}**")

    if s["storage"]:
        lines = [
            f"{ICONS[disk_levels[name]]} `{bar(v)}` **{v:.0f} %** — {name}"
            for name, v in s["storage"].items()
        ]
        embed.add_field(name="💿 Stockage", value="\n".join(lines)[:1024], inline=False)

    running = sum(1 for g in s["guests"] if g["running"])
    embed.add_field(
        name=f"📦 Invités ({running}/{len(s['guests'])} en marche)",
        value=guests_text(s["guests"]) or "Aucun",
        inline=False,
    )
    embed.set_footer(text=f"Nœud {PVE_NODE} • uptime {fmt_uptime(s['uptime'])}")
    return embed


# --------------------------------------------------------------------------- actions Proxmox
async def pve_action(session: aiohttp.ClientSession, guest: dict, action: str) -> None:
    if not (PVE_TOKEN_ID and PVE_TOKEN_SECRET):
        raise RuntimeError("Token Proxmox non configuré (PVE_TOKEN_ID / PVE_TOKEN_SECRET).")
    url = f"{PVE_URL}/api2/json/nodes/{PVE_NODE}/{guest['type']}/{guest['vmid']}/status/{action}"
    headers = {"Authorization": f"PVEAPIToken={PVE_TOKEN_ID}={PVE_TOKEN_SECRET}"}
    async with session.post(url, headers=headers, ssl=PVE_VERIFY_SSL) as resp:
        body = await resp.text()
        if resp.status != 200:
            raise RuntimeError(f"Proxmox a répondu HTTP {resp.status} : {body[:300]}")


class Confirm(discord.ui.View):
    def __init__(self, author_id: int):
        super().__init__(timeout=30)
        self.author_id = author_id
        self.confirmed: bool | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.author_id

    @discord.ui.button(label="Confirmer", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        await interaction.response.edit_message(content="⏳ Envoi de la commande à Proxmox…", view=None)
        self.stop()

    @discord.ui.button(label="Annuler", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = False
        await interaction.response.edit_message(content="Action annulée.", view=None)
        self.stop()


# --------------------------------------------------------------------------- bot
class AlertTracker:
    def __init__(self):
        self.counts: dict[str, int] = {}
        self.active: dict[str, float] = {}  # clé d'alerte -> moment du dernier envoi
        self.guest_state: dict[str, bool] | None = None
        self.prom_failures = 0
        self.prom_down_alerted = False


class HomelabBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.session: aiohttp.ClientSession | None = None
        self.prom: Prometheus | None = None

    async def setup_hook(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        self.prom = Prometheus(self.session)
        self.tree.copy_global_to(guild=GUILD)
        await self.tree.sync(guild=GUILD)
        alert_loop.start()
        daily_report.start()

    async def on_ready(self):
        log.info("Connecté en tant que %s", self.user)

    async def close(self):
        if self.session:
            await self.session.close()
        await super().close()


bot = HomelabBot()
tracker = AlertTracker()


async def get_channel() -> discord.abc.Messageable:
    return bot.get_channel(CHANNEL_ID) or await bot.fetch_channel(CHANNEL_ID)


async def send_alert(title: str, description: str, color: int, mention: bool = True) -> None:
    try:
        channel = await get_channel()
        embed = discord.Embed(title=title, description=description, color=color, timestamp=datetime.now(TZ))
        content = ALERT_MENTION if (mention and ALERT_MENTION) else None
        await channel.send(
            content=content,
            embed=embed,
            allowed_mentions=discord.AllowedMentions(everyone=True, users=True, roles=True),
        )
    except Exception:
        log.exception("Impossible d'envoyer l'alerte « %s »", title)


# --------------------------------------------------------------------------- tâches automatiques
@tasks.loop(seconds=CHECK_INTERVAL)
async def alert_loop():
    try:
        await check_alerts()
    except Exception:
        log.exception("Erreur pendant la vérification des alertes")


@alert_loop.before_loop
async def _wait_alerts():
    await bot.wait_until_ready()


async def check_alerts() -> None:
    try:
        s = await collect(bot.prom)
    except Exception as exc:
        tracker.prom_failures += 1
        log.warning("Prometheus injoignable (%s) : %s", tracker.prom_failures, exc)
        if tracker.prom_failures >= 3 and not tracker.prom_down_alerted:
            tracker.prom_down_alerted = True
            await send_alert(
                "🔴 Prometheus injoignable",
                f"Impossible de lire les métriques depuis {tracker.prom_failures} minutes.\n`{exc}`",
                RED,
            )
        return

    if tracker.prom_down_alerted:
        await send_alert("✅ Prometheus de nouveau joignable", "La supervision reprend.", GREEN, mention=False)
    tracker.prom_failures = 0
    tracker.prom_down_alerted = False

    metrics = {
        "cpu": ("CPU", s["cpu"], "%"),
        "ram": ("RAM", s["ram"], "%"),
        "temp": ("Température", s["temp"], "°C"),
    }
    for name, value in s["storage"].items():
        metrics[f"disk:{name}"] = (f"Stockage {name}", value, "%")

    now = time.monotonic()
    for key, (label, value, unit) in metrics.items():
        if value is None:
            continue
        threshold = THRESHOLDS[key.split(":")[0]]
        if value >= threshold:
            tracker.counts[key] = tracker.counts.get(key, 0) + 1
            if tracker.counts[key] >= ALERT_CHECKS:
                last = tracker.active.get(key)
                if last is None or now - last >= ALERT_COOLDOWN:
                    tracker.active[key] = now
                    await send_alert(
                        f"🔴 Alerte {label}",
                        f"{label} à **{value:.0f} {unit}** depuis environ {tracker.counts[key]} min "
                        f"(seuil : {threshold:.0f} {unit}).",
                        RED,
                    )
        else:
            tracker.counts[key] = 0
            if key in tracker.active:
                del tracker.active[key]
                await send_alert(
                    f"✅ {label} revenu à la normale",
                    f"{label} à **{value:.0f} {unit}**.",
                    GREEN,
                    mention=False,
                )

    current = {g["id"]: g for g in s["guests"]}
    if tracker.guest_state is not None:
        for gid, g in current.items():
            before = tracker.guest_state.get(gid)
            if before is True and not g["running"]:
                await send_alert(
                    f"🔴 {g['vmid']} {g['name']} s'est arrêté",
                    "L'invité n'est plus en marche.",
                    RED,
                )
            elif before is False and g["running"]:
                await send_alert(
                    f"✅ {g['vmid']} {g['name']} est de nouveau en marche",
                    "L'invité a redémarré.",
                    GREEN,
                    mention=False,
                )
    tracker.guest_state = {gid: g["running"] for gid, g in current.items()}


@tasks.loop(time=dtime(hour=REPORT_HOUR, minute=0, tzinfo=TZ))
async def daily_report():
    try:
        s = await collect(bot.prom)
        channel = await get_channel()
        await channel.send(embed=status_embed(s, title="☀️ Rapport quotidien du homelab"))
    except Exception:
        log.exception("Échec du rapport quotidien")


@daily_report.before_loop
async def _wait_report():
    await bot.wait_until_ready()


# --------------------------------------------------------------------------- commandes
@bot.tree.command(name="status", description="État du serveur : CPU, RAM, température, stockage, invités")
async def status_cmd(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        s = await collect(bot.prom)
    except Exception as exc:
        await interaction.followup.send(f"❌ Impossible de lire les métriques : `{exc}`")
        return
    await interaction.followup.send(embed=status_embed(s))


@bot.tree.command(name="action", description="Démarrer, arrêter ou redémarrer un conteneur ou une VM autorisé")
@app_commands.describe(invite="Conteneur ou VM", action="Action à exécuter")
@app_commands.choices(action=[app_commands.Choice(name=label, value=key) for key, label in ACTIONS.items()])
async def action_cmd(interaction: discord.Interaction, invite: int, action: app_commands.Choice[str]):
    if interaction.user.id not in ADMIN_IDS:
        await interaction.response.send_message("⛔ Commande réservée aux administrateurs du bot.", ephemeral=True)
        return
    if invite not in ALLOWED_GUESTS:
        await interaction.response.send_message(
            "⛔ Cet invité n'est pas autorisé (variable ALLOWED_GUESTS).", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        s = await collect(bot.prom)
    except Exception as exc:
        await interaction.followup.send(f"❌ Impossible de lire l'état des invités : `{exc}`", ephemeral=True)
        return

    guest = next((g for g in s["guests"] if g["vmid"] == invite), None)
    if guest is None:
        await interaction.followup.send(f"❌ Invité {invite} introuvable.", ephemeral=True)
        return

    label = ACTIONS[action.value]
    target = f"**{guest['vmid']} {guest['name']}**"

    if action.value != "start":
        view = Confirm(interaction.user.id)
        msg = await interaction.followup.send(
            f"⚠️ Confirmer **{label}** de {target} ?", view=view, ephemeral=True, wait=True
        )
        await view.wait()
        if view.confirmed is None:
            await msg.edit(content="⌛ Délai dépassé, action annulée.", view=None)
            return
        if not view.confirmed:
            return

    try:
        await pve_action(bot.session, guest, action.value)
    except Exception as exc:
        await interaction.followup.send(f"❌ Échec : `{exc}`", ephemeral=True)
        return

    await interaction.followup.send(f"✅ {label} de {target} demandé à Proxmox.", ephemeral=True)
    try:
        channel = await get_channel()
        await channel.send(embed=discord.Embed(
            title=f"🛠️ {label} : {guest['vmid']} {guest['name']}",
            description=f"Demandé par {interaction.user.mention}",
            color=ORANGE,
            timestamp=datetime.now(TZ),
        ))
    except Exception:
        log.exception("Impossible de journaliser l'action dans le salon")


@action_cmd.autocomplete("invite")
async def invite_autocomplete(interaction: discord.Interaction, current: str):
    try:
        s = await collect(bot.prom)
    except Exception:
        return []
    cur = current.lower()
    return [
        app_commands.Choice(
            name=f"{g['vmid']} — {g['name']} ({'en marche' if g['running'] else 'arrêté'})",
            value=g["vmid"],
        )
        for g in s["guests"]
        if g["vmid"] in ALLOWED_GUESTS and cur in f"{g['vmid']} {g['name']}".lower()
    ][:25]


@bot.tree.command(name="aide", description="Liste des commandes et des seuils d'alerte")
async def aide_cmd(interaction: discord.Interaction):
    embed = discord.Embed(title="📖 Aide du bot homelab", color=GREY)
    embed.add_field(
        name="Commandes",
        value=(
            "`/status` : état du serveur et des invités\n"
            "`/action` : démarrer / arrêter / redémarrer un invité autorisé (admins)\n"
            "`/aide` : ce message"
        ),
        inline=False,
    )
    embed.add_field(
        name="Alertes automatiques",
        value=(
            f"CPU ≥ {THRESHOLDS['cpu']:.0f} % · RAM ≥ {THRESHOLDS['ram']:.0f} % · "
            f"Disque ≥ {THRESHOLDS['disk']:.0f} % · Température ≥ {THRESHOLDS['temp']:.0f} °C\n"
            f"Déclenchement après {ALERT_CHECKS} vérification(s) consécutive(s), "
            f"rappel toutes les {ALERT_COOLDOWN // 60} min si le problème persiste.\n"
            "Alerte aussi si un invité s'arrête ou si Prometheus devient injoignable."
        ),
        inline=False,
    )
    embed.add_field(name="Rapport quotidien", value=f"Tous les jours à {REPORT_HOUR} h.", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    log.error("Erreur de commande : %s", error, exc_info=error)
    message = "❌ Une erreur est survenue, consulte les logs du bot (`docker logs homelab-bot`)."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN, log_handler=None)
