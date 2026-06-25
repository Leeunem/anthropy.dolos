#!/usr/bin/env python3
"""
Le jeu de l'imposteur — version web (FastAPI + WebSocket).  v0.1.2

L'application sert de « distributeur de rôles » : à chaque clic sur « Nouvelle
partie », elle tire au sort un imposteur parmi les joueurs connectés, choisit un
mot dans la bibliothèque (selon le niveau de difficulté retenu par l'hôte), puis
affiche « Vous êtes l'imposteur » à l'intrus et le mot secret à tous les autres.

Nouveautés 0.1.2 :
  • Sessions persistantes : chaque joueur reçoit un jeton conservé côté
    navigateur. À la reconnexion (réveil du téléphone), le jeton le rebranche
    sur son Player existant — pseudo, rôle et place préservés — avec un délai
    de grâce de 60 s avant tout retrait définitif.
  • Sélecteur de difficulté (3 niveaux cumulatifs) choisi avant chaque partie.

Un seul port HTTP : page web et WebSockets passent par le même service (PORT).

Lancement local :
    pip install -r requirements.txt
    python app.py
    -> http://127.0.0.1:5000
"""

import os
import asyncio
import random
import secrets
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

__version__ = "0.1.2"

# --- Réglages du jeu (modifiables) ------------------------------------------
MIN_PLAYERS = 3
GRACE_SECONDS = int(os.environ.get("GRACE_SECONDS", "60"))  # un déconnecté reste « absent » ce temps

# Niveaux de difficulté CUMULATIFS, exprimés par un seuil de fréquence minimal
# (occurrences/million, films — source Lexique). Chaque niveau englobe le
# précédent : plus le niveau monte, plus le vivier de mots s'élargit vers des
# termes rares, abstraits ou retors.
#   1 — mots courants            (les plus fréquents)
#   2 — mots courants + concepts (ajoute le vocabulaire plus abstrait)
#   3 — toute la base de mots
LEVELS = {1: 8.0, 2: 2.0, 3: 0.0}


def _default_level() -> int:
    """Niveau par défaut : DIFFICULTY_LEVEL (1/2/3), sinon mappe l'ancien
    DIFFICULTY (facile/moyen/difficile/tout), sinon 2."""
    raw = os.environ.get("DIFFICULTY_LEVEL")
    if raw:
        try:
            lv = int(raw)
            if lv in LEVELS:
                return lv
        except ValueError:
            pass
    legacy = {"facile": 1, "moyen": 2, "difficile": 3, "tout": 3}
    return legacy.get(os.environ.get("DIFFICULTY", "").lower(), 2)


DEFAULT_LEVEL = _default_level()

# Filet de sécurité si le corpus Lexique est absent.
FALLBACK_WORDS = [
    "plage", "montagne", "café", "bibliothèque", "orage", "violon",
    "aéroport", "marché", "horloge", "phare", "vignoble", "métro",
    "cuisine", "désert", "cinéma", "jardin", "tempête", "carnaval",
]


def load_corpus() -> list[tuple[str, float]]:
    """Charge (mot, fréquence) depuis words_fr.txt (voir build_words.py).

    On peut forcer une liste sur mesure via la variable d'environnement WORDS
    (mots séparés par des virgules), disponible alors à tous les niveaux.
    """
    if os.environ.get("WORDS"):
        return [(w.strip(), 1e9) for w in os.environ["WORDS"].split(",") if w.strip()]
    try:
        lines = (Path(__file__).parent / "words_fr.txt").read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return [(w, 1e9) for w in FALLBACK_WORDS]
    corpus: list[tuple[str, float]] = []
    for line in lines:
        if not line or line.startswith("#"):
            continue
        ortho, _, freq = line.partition("\t")
        try:
            corpus.append((ortho, float(freq)))
        except ValueError:
            continue
    return corpus or [(w, 1e9) for w in FALLBACK_WORDS]


CORPUS = load_corpus()


def pool_for_level(level: int) -> list[str]:
    """Vivier de mots pour un niveau donné (cumulatif)."""
    threshold = LEVELS.get(level, LEVELS[DEFAULT_LEVEL])
    pool = [w for (w, f) in CORPUS if f >= threshold]
    return pool or FALLBACK_WORDS


INDEX_HTML = (Path(__file__).parent / "index.html").read_text(encoding="utf-8")

app = FastAPI()


# ----------------------------------------------------------------------------
# Modèle
# ----------------------------------------------------------------------------
class Player:
    """Identité durable, découplée de la connexion : c'est le jeton (token),
    non le socket, qui définit le joueur."""

    def __init__(self, token: str, pid: int):
        self.token = token
        self.pid = pid
        self.ws: WebSocket | None = None
        self.name: str | None = None
        self.impostor = False
        self.word: str | None = None      # mémorisé pour resservir le rôle au retour
        self.connected = False
        self._grace: asyncio.Task | None = None  # tâche de retrait différé

    async def send(self, **obj):
        if self.connected and self.ws is not None:
            try:
                await self.ws.send_json(obj)
            except Exception:
                self.connected = False


class Room:
    """Une partie partagée en mémoire (une à la fois, comme l'original)."""

    def __init__(self):
        self.players: dict[str, Player] = {}   # clé = jeton
        self.state = "lobby"                    # lobby | revealed
        self.host_token: str | None = None
        self.last_word: str | None = None
        self.level = DEFAULT_LEVEL              # dernier niveau de difficulté choisi

    # --- joueurs ---
    def active(self):
        """Joueurs présents (connectés et nommés)."""
        return [p for p in self.players.values() if p.connected and p.name]

    def named(self):
        """Tous les joueurs nommés, présents ou en délai de grâce."""
        return [p for p in self.players.values() if p.name]

    def name_taken(self, name, exclude: Player | None = None):
        return any(p.name == name for p in self.named() if p is not exclude)

    def _lobby_payload(self, player: "Player") -> dict:
        roster = [{"name": p.name, "absent": not p.connected} for p in self.named()]
        connected = sum(1 for p in roster if not p["absent"])
        return dict(
            type="lobby",
            players=roster,
            host=(player.token == self.host_token),
            canStart=connected >= MIN_PLAYERS,
            minPlayers=MIN_PLAYERS,
            level=self.level,
        )

    async def push_lobby(self):
        for p in self.active():
            await p.send(**self._lobby_payload(p))

    async def send_lobby(self, player: "Player"):
        await player.send(**self._lobby_payload(player))

    # --- la séquence d'un tour : tirage + révélation ---
    async def deal(self, level: int):
        """« Nouvelle partie » → 1 imposteur au hasard, 1 mot, révélation à tous."""
        players = self.active()
        if len(players) < MIN_PLAYERS:
            return
        self.level = level if level in LEVELS else self.level
        self.state = "revealed"

        impostor = random.choice(players)
        pool = pool_for_level(self.level)
        word = random.choice([w for w in pool if w != self.last_word] or pool)
        self.last_word = word

        for p in players:
            p.impostor = p is impostor
            p.word = None if p.impostor else word
            await p.send(
                type="role",
                impostor=p.impostor,
                word=p.word,
                host=(p.token == self.host_token),
                level=self.level,
            )


room = Room()
_next_pid = 0


# ----------------------------------------------------------------------------
# Délai de grâce
# ----------------------------------------------------------------------------
def cancel_removal(player: Player):
    if player._grace and not player._grace.done():
        player._grace.cancel()
    player._grace = None


def schedule_removal(player: Player):
    cancel_removal(player)
    player._grace = asyncio.create_task(_remove_after_grace(player))


async def _remove_after_grace(player: Player):
    try:
        await asyncio.sleep(GRACE_SECONDS)
    except asyncio.CancelledError:
        return
    if player.connected:           # revenu entre-temps : on ne touche à rien
        return
    room.players.pop(player.token, None)
    if room.host_token == player.token:
        remaining = room.active()
        room.host_token = remaining[0].token if remaining else None
    if room.state == "lobby":
        await room.push_lobby()


# ----------------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index():
    return INDEX_HTML


@app.get("/health")
async def health():
    return {"ok": True, "version": __version__}


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    player: Player | None = None
    try:
        while True:
            msg = await websocket.receive_json()
            player = await handle(websocket, player, msg)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        # On ne ferme la session que si ce socket est bien le socket courant
        # du joueur (évite de couper une reconnexion arrivée entre-temps).
        if player is not None and player.ws is websocket:
            player.connected = False
            player.ws = None
            schedule_removal(player)
            if room.state == "lobby":
                await room.push_lobby()


async def handle(websocket: WebSocket, player: Player | None, msg: dict) -> Player | None:
    global _next_pid
    t = msg.get("type")

    if t == "join":
        token = msg.get("token")

        # --- Reconnexion : jeton connu → on rebranche le Player existant ---
        if token and token in room.players:
            player = room.players[token]
            cancel_removal(player)
            player.ws = websocket
            player.connected = True
            await player.send(type="session", token=player.token)
            if room.state == "revealed":
                await player.send(
                    type="role",
                    impostor=player.impostor,
                    word=player.word,
                    host=(player.token == room.host_token),
                    level=room.level,
                )
                await room.push_lobby()  # rafraîchit le « absent » chez les autres
            else:
                await room.push_lobby()
            return player

        # --- Nouvelle session ---
        name = (msg.get("name") or "").strip()[:20]
        if not name:
            await websocket.send_json({"type": "error", "text": "Choisis un pseudo."})
            return player
        if room.name_taken(name):
            await websocket.send_json({"type": "error", "text": "Ce pseudo est déjà pris."})
            return player
        _next_pid += 1
        token = secrets.token_urlsafe(12)
        player = Player(token, _next_pid)
        player.ws = websocket
        player.name = name
        player.connected = True
        room.players[token] = player
        if room.host_token is None:
            room.host_token = token
        await player.send(type="session", token=token)
        if room.state == "lobby":
            await room.push_lobby()
        else:
            await room.send_lobby(player)  # arrivé en pleine manche : prochaine fois
        return player

    elif t == "newgame":
        if player and player.token == room.host_token:
            await room.deal(int(msg.get("level", room.level)))
        return player

    return player


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 5000))
    uvicorn.run(app, host="0.0.0.0", port=port)
