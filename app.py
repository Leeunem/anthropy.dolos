#!/usr/bin/env python3
"""
Le jeu de l'imposteur — version web (FastAPI + WebSocket).

Tout le monde reçoit le même mot secret, sauf une personne tirée au sort :
l'imposteur. À tour de rôle, chacun donne un mot lié au mot secret. L'imposteur
improvise pour se fondre, puis tout le monde vote.

Un seul port HTTP : la page web et les WebSockets passent par le même service,
ce que Render attend. Le port est lu dans la variable d'environnement PORT.

Lancement local :
    pip install -r requirements.txt
    python app.py
    -> http://127.0.0.1:5000
"""

import asyncio
import os
import random
from collections import Counter
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

# --- Réglages du jeu (modifiables) ------------------------------------------
ROUNDS = int(os.environ.get("ROUNDS", 2))
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
        self.state = "lobby"          # lobby | playing | vote | result
        self.host_pid: int | None = None
        self.clue_future: asyncio.Future | None = None
        self.clue_pid: int | None = None
        self.votes: dict[int, str] = {}
        self.expected_votes = 0
        self.vote_event: asyncio.Event | None = None
        self.last_word: str | None = None

    # --- joueurs présents ---
    def active(self):
        return [p for p in self.players.values() if p.connected and p.name]

    def name_taken(self, name):
        return any(p.name == name for p in self.active())

    async def broadcast(self, **obj):
        for p in self.active():
            await p.send(**obj)

    async def push_lobby(self):
        names = [p.name for p in self.active()]
        for p in self.active():
            await p.send(
                type="lobby",
                players=names,
                host=(p.pid == self.host_pid),
                canStart=len(names) >= MIN_PLAYERS,
                minPlayers=MIN_PLAYERS,
            )

    # --- déroulé de la partie ---
    async def start(self):
        players = self.active()
        if self.state != "lobby" or len(players) < MIN_PLAYERS:
            return
        self.state = "playing"

        word = random.choice([w for w in WORDS if w != self.last_word] or WORDS)
        self.last_word = word
        impostor = random.choice(players)
        for p in players:
            p.impostor = p is impostor
            await p.send(
                type="role",
                impostor=p.impostor,
                word=None if p.impostor else word,
            )

        await asyncio.sleep(0.2)

        for rnd in range(1, ROUNDS + 1):
            for p in players:
                if not p.connected:
                    continue
                await self.broadcast(
                    type="turn", current=p.name, round=rnd, totalRounds=ROUNDS
                )
                value = await self._await_clue(p)
                await self.broadcast(type="clue", player=p.name, value=value)

        await self._vote(players, impostor)

    async def _await_clue(self, player: Player) -> str:
        loop = asyncio.get_running_loop()
        self.clue_future = loop.create_future()
        self.clue_pid = player.pid
        try:
            value = await self.clue_future
        except asyncio.CancelledError:
            value = "(absent)"
        self.clue_future = None
        self.clue_pid = None
        return value or "(rien)"

    async def _vote(self, players, impostor):
        self.state = "vote"
        self.votes = {}
        names = [p.name for p in players if p.connected]
        self.expected_votes = len(names)
        self.vote_event = asyncio.Event()
        await self.broadcast(type="vote", options=names)
        await self.vote_event.wait()
        await self._result(impostor)

    async def _result(self, impostor: Player):
        self.state = "result"
        tally = Counter(self.votes.values())
        voted_out = None
        if tally:
            top = max(tally.values())
            leaders = [n for n, c in tally.items() if c == top]
            voted_out = leaders[0] if len(leaders) == 1 else None  # égalité = personne

        caught = voted_out == impostor.name
        if voted_out is None:
            text = "Égalité au vote : personne n'est démasqué. L'imposteur s'en sort."
        elif caught:
            text = f"« {voted_out} » était bien l'imposteur. Bien joué !"
        else:
            text = f"Raté : « {voted_out} » était innocent. L'imposteur a gagné."

        await self.broadcast(
            type="result",
            impostor=impostor.name,
            votedOut=voted_out,
            caught=caught,
            tie=(voted_out is None and bool(tally)),
            tally=dict(tally),
            text=text,
        )

    def reset(self):
        self.state = "lobby"
        self.votes = {}
        for p in self.players.values():
            p.impostor = False


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
        # libère un tour ou un vote bloqué par ce départ
        if room.clue_pid == player.pid and room.clue_future and not room.clue_future.done():
            room.clue_future.cancel()
        if room.state == "vote" and room.vote_event and len(room.votes) >= len(room.active()):
            room.vote_event.set()
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
        if room.state != "lobby":
            await player.send(type="error", text="Une partie est en cours. Patiente.")
            return
        if room.name_taken(name):
            await player.send(type="error", text="Ce pseudo est déjà pris.")
            return
        player.name = name
        if room.host_pid is None:
            room.host_pid = player.pid
        await room.push_lobby()

    elif t == "start":
        if player.pid == room.host_pid:
            asyncio.create_task(room.start())

    elif t == "clue":
        if (
            room.clue_pid == player.pid
            and room.clue_future
            and not room.clue_future.done()
        ):
            room.clue_future.set_result((msg.get("value") or "").strip()[:40])

    elif t == "vote":
        if room.state == "vote" and player.name and player.name not in room.votes:
            choice = msg.get("value")
            if choice:
                room.votes[player.name] = choice
                if len(room.votes) >= room.expected_votes and room.vote_event:
                    room.vote_event.set()

    elif t == "again":
        if player.pid == room.host_pid and room.state == "result":
            room.reset()
            await room.push_lobby()


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 5000))
    uvicorn.run(app, host="0.0.0.0", port=port)
