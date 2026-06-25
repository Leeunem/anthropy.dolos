# Journal des modifications — Le jeu de l'imposteur

## [0.1.2] — 2026-06-25

### Ajouté
- **Sessions persistantes (jeton de reconnexion).** À la première connexion, le
  serveur attribue à chaque joueur un jeton unique, conservé côté client dans
  `localStorage`. À la reconnexion — réveil du téléphone, perte de réseau — le
  navigateur renvoie ce jeton : le serveur rebranche le joueur sur son `Player`
  existant (même pseudo, même rôle, même place dans la partie) au lieu d'en
  créer un nouveau. La reconnexion est automatique, avec relance au retour de
  l'onglet au premier plan et un bandeau « Reconnexion… ».
- **Délai de grâce de 60 s.** Une déconnexion ne retire plus le joueur
  immédiatement : il est marqué « absent » pendant 60 secondes (durée réglable
  via la variable d'environnement `GRACE_SECONDS`). S'il revient à temps, rien
  n'est perdu ; sinon il est retiré proprement et l'hôte est réattribué au
  besoin. Les joueurs absents apparaissent estompés dans le salon.
- **Sélecteur de difficulté (3 niveaux) avant chaque partie.** L'hôte choisit le
  vivier de mots dans lequel le mot secret est tiré :
  - **Niveau 1 — Courant** : uniquement les mots les plus fréquents.
  - **Niveau 2 — Courant + concepts** : ajoute le vocabulaire plus abstrait.
  - **Niveau 3 — Tout** : l'ensemble de la base de mots.
  Les niveaux sont cumulatifs (chacun englobe le précédent).

### Modifié
- L'identité d'un joueur repose désormais sur son **jeton** (durable) et non plus
  sur sa connexion WebSocket : `Room` indexe les `Player` par jeton, l'hôte est
  suivi par `host_token`.
- La difficulté, auparavant figée par la variable d'environnement `DIFFICULTY`,
  est **choisie par partie** depuis l'interface. La variable `DIFFICULTY_LEVEL`
  (1/2/3) sert de valeur par défaut ; l'ancien `DIFFICULTY` reste reconnu en
  repli.
- `render.yaml` utilise `DIFFICULTY_LEVEL` en remplacement de `DIFFICULTY`.

### Fichiers touchés
- `app.py` · `index.html` · `render.yaml`
