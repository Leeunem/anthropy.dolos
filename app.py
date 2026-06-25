#!/usr/bin/env python3
"""
Le jeu de l'imposteur — version web (FastAPI + WebSocket).

L'application sert de « distributeur de rôles » : à chaque clic sur « Nouvelle
partie », elle tire au sort un imposteur parmi les joueurs connectés, choisit un
mot dans la bibliothèque, puis affiche « Vous êtes l'imposteur » à l'intrus et le
mot secret à tous les autres. La discussion, les indices et l'accusation se font
ensuite de vive voix entre les joueurs.

Un seul port HTTP : la page web et les WebSockets passent par le même service,
ce que Render attend. Le port est lu dans la variable d'environnement PORT.

Lancement local :
    pip install -r requirements.txt
    python app.py
    -> http://127.0.0.1:5000
"""

import os
import random
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

# --- Réglages du jeu (modifiables) ------------------------------------------
MIN_PLAYERS = 3

# Niveau de difficulté → bande de fréquence (occurrences/million dans les films).
# Mots courants = faciles à deviner ; mots rares = plus retors pour l'imposteur.
DIFFICULTY = os.environ.get("DIFFICULTY", "moyen").lower()
BANDS = {
    "facile": (8.0, float("inf")),
    "moyen": (2.0, 8.0),
    "difficile": (0.5, 2.0),
    "tout": (0.0, float("inf")),
}

# Filet de sécurité si le corpus Lexique est absent.
FALLBACK_WORDS = [
    "plage", "montagne", "café", "bibliothèque", "orage", "violon",
    "aéroport", "marché", "horloge", "phare", "vignoble", "métro",
    "cuisine", "désert", "cinéma", "jardin", "tempête", "carnaval",
]


def load_words() -> list[str]:
    """Corpus issu de Lexique 383 (voir build_words.py), filtré par difficulté.

    On peut forcer une liste sur mesure via la variable d'environnement WORDS
    (mots séparés par des virgules) ; sinon on lit words.fr.txt.
    """
    if os.environ.get("WORDS"):
        return [w.strip() for w in os.environ["WORDS"].split(",") if w.strip()]

    lo, hi = BANDS.get(DIFFICULTY, BANDS["moyen"])
    pool: list[str] = []
    try:
        lines = (Path(__file__).parent / "words_fr.txt").read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return FALLBACK_WORDS
    for line in lines:
        if not line or line.startswith("#"):
            continue
        ortho, _, freq = line.partition("\t")
        try:
            if lo <= float(freq) < hi:
                pool.append(ortho)
        except ValueError:
            continue
    return pool or FALLBACK_WORDS


WORDS = load_words()

INDEX_HTML = (Path(__file__).parent / "index.html").read_text(encoding="utf-8")

app = FastAPI()


# ----------------------------------------------------------------------------
# Modèle
# ----------------------------------------------------------------------------
class Player:
    def __init__(self, ws: WebSocket, pid: int):
        self.ws = ws
        self.pid = pid
        self.name = None
        self.impostor = False
        self.connected = True

    async def send(self, **obj):
        if self.connected:
            try:
                await self.ws.send_json(obj)
            except Exception:
                self.connected = False


class Room:
    """Une partie partagée en mémoire (une à la fois, comme l'original)."""

    def __init__(self):
        self.players: dict[int, Player] = {}
        self.state = "lobby"          # lobby | revealed
        self.host_pid: int | None = None
        self.last_word: str | None = None

    # --- joueurs présents ---
    def active(self):
        return [p for p in self.players.values() if p.connected and p.name]

    def name_taken(self, name):
        return any(p.name == name for p in self.active())

    def _lobby_payload(self, player: "Player") -> dict:
        names = [p.name for p in self.active()]
        return dict(
            type="lobby",
            players=names,
            host=(player.pid == self.host_pid),
            canStart=len(names) >= MIN_PLAYERS,
            minPlayers=MIN_PLAYERS,
        )

    async def push_lobby(self):
        for p in self.active():
            await p.send(**self._lobby_payload(p))

    async def send_lobby(self, player: "Player"):
        """État du salon pour un seul joueur (ex. arrivée pendant une manche)."""
        await player.send(**self._lobby_payload(player))

    # --- la séquence d'un tour : tirage + révélation ---
    async def deal(self):
        """« Nouvelle partie » → 1 imposteur au hasard, 1 mot, révélation à tous."""
        players = self.active()
        if len(players) < MIN_PLAYERS:
            return
        self.state = "revealed"

        # 2. un imposteur tiré au sort parmi les connectés
        impostor = random.choice(players)
        # 3. un mot tiré de la bibliothèque (différent du précédent)
        word = random.choice([w for w in WORDS if w != self.last_word] or WORDS)
        self.last_word = word

        # 4. « vous êtes imposteur » à l'un, le mot à tous les autres
        for p in players:
            p.impostor = p is impostor
            await p.send(
                type="role",
                impostor=p.impostor,
                word=None if p.impostor else word,
                host=(p.pid == self.host_pid),
            )


room = Room()
_next_pid = 0


# ----------------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index():
    return INDEX_HTML


@app.get("/health")
async def health():
    return {"ok": True}


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    global _next_pid
    await websocket.accept()
    _next_pid += 1
    player = Player(websocket, _next_pid)
    room.players[player.pid] = player

    try:
        while True:
            msg = await websocket.receive_json()
            await handle(player, msg)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        player.connected = False
        room.players.pop(player.pid, None)
        if room.host_pid == player.pid:
            remaining = room.active()
            room.host_pid = remaining[0].pid if remaining else None
        if room.state == "lobby":
            await room.push_lobby()


async def handle(player: Player, msg: dict):
    t = msg.get("type")

    if t == "join":
        name = (msg.get("name") or "").strip()[:20]
        if not name:
            await player.send(type="error", text="Choisis un pseudo.")
            return
        if room.name_taken(name):
            await player.send(type="error", text="Ce pseudo est déjà pris.")
            return
        player.name = name
        if room.host_pid is None:
            room.host_pid = player.pid
        if room.state == "lobby":
            await room.push_lobby()
        else:
            # une manche est déjà révélée : ce joueur entrera à la prochaine
            await room.send_lobby(player)

    elif t == "newgame":
        if player.pid == room.host_pid:
            await room.deal()


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 5000))
    uvicorn.run(app, host="0.0.0.0", port=port)
