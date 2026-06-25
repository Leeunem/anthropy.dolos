#!/usr/bin/env python3
"""
Génère le corpus de mots du jeu à partir de la base lexicale Lexique 383
(http://www.lexique.org).

On ne garde que des noms communs « jouables » : lemmes au singulier, de 4 à
10 lettres, sans nom propre ni mot composé, et suffisamment fréquents pour
être reconnus. La fréquence (occurrences par million dans les sous-titres de
films) est conservée afin de proposer des niveaux de difficulté.

Usage :
    python build_words.py                  # télécharge Lexique et écrit words.fr.txt
    python build_words.py --file X.csv     # part d'un fichier local déjà téléchargé
    python build_words.py --out chemin.txt # change le fichier de sortie

Le script accepte indifféremment le .tsv officiel (tabulations, UTF-8) et les
copies au format « ; » / Latin-1 : il détecte séparateur, encodage et noms de
colonnes automatiquement.
"""

import argparse
import csv
import io
import re
import sys
import urllib.request

SOURCES = [
    "http://www.lexique.org/databases/Lexique383/Lexique383.tsv",
]

# Caractères français acceptés (minuscules uniquement → exclut les noms propres)
VALID = re.compile(r"^[a-zàâäéèêëîïôöùûüçœæ]+$")

MIN_LEN, MAX_LEN = 4, 10
MIN_FREQ = 0.5          # plancher d'occurrences/million pour rester reconnaissable

# Colonnes utiles, repérées par leur nom canonique (insensible à un préfixe « N_ »)
NEEDED = ("ortho", "cgram", "genre", "freqlemfilms2", "islem", "nblettres")


def fetch(file: str | None) -> bytes:
    if file:
        with open(file, "rb") as f:
            return f.read()
    last = None
    for url in SOURCES:
        try:
            sys.stderr.write(f"Téléchargement de Lexique : {url}\n")
            with urllib.request.urlopen(url, timeout=60) as r:
                return r.read()
        except Exception as exc:               # noqa: BLE001
            last = exc
            sys.stderr.write(f"  échec : {exc}\n")
    raise SystemExit(f"Impossible de récupérer Lexique ({last}).")


def decode(raw: bytes) -> str:
    for enc in ("utf-8", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


def normalise(name: str) -> str:
    """« 7_freqlemfilms2 » → « freqlemfilms2 », insensible à la casse."""
    return re.sub(r"^\d+_", "", name.strip().lower())


def build(raw: bytes):
    text = decode(raw)
    delimiter = "\t" if "\t" in text.splitlines()[0] else ";"
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    header = [normalise(h) for h in next(reader)]
    idx = {c: header.index(c) for c in NEEDED if c in header}
    missing = [c for c in NEEDED if c not in idx]
    if missing:
        raise SystemExit(f"Colonnes absentes du fichier source : {missing}")

    seen, words = set(), []
    for row in reader:
        if len(row) <= max(idx.values()):
            continue
        ortho = row[idx["ortho"]].strip()
        if (
            row[idx["cgram"]] != "NOM"
            or row[idx["islem"]] != "1"
            or row[idx["genre"]] not in ("m", "f")
            or not VALID.match(ortho)
            or ortho in seen
        ):
            continue
        try:
            freq = float(row[idx["freqlemfilms2"]].replace(",", "."))
            length = int(row[idx["nblettres"]])
        except ValueError:
            continue
        if MIN_LEN <= length <= MAX_LEN and freq >= MIN_FREQ:
            seen.add(ortho)
            words.append((ortho, freq))

    words.sort(key=lambda w: -w[1])            # du plus fréquent au plus rare
    return words


def main():
    ap = argparse.ArgumentParser(description="Construit words.fr.txt depuis Lexique 383.")
    ap.add_argument("--file", help="fichier Lexique local (sinon téléchargement)")
    ap.add_argument("--out", default="words.fr.txt", help="fichier de sortie")
    args = ap.parse_args()

    words = build(fetch(args.file))
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("# mot<TAB>fréquence (occurrences/million, films) — source : lexique.org\n")
        for ortho, freq in words:
            f.write(f"{ortho}\t{freq:g}\n")

    sys.stderr.write(f"{len(words)} mots écrits dans {args.out}\n")


if __name__ == "__main__":
    main()
