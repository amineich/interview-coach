#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
interview-coach — entraînement aux entretiens d'embauche, 100 % local.

Écoute le micro (ou un fichier .wav), transcrit la question entendue (anglais ou
français, langue détectée automatiquement), la rapproche de la base personnelle
questions.md (toujours en anglais) et affiche la réponse préparée DANS LA LANGUE
DE L'AUDIO.

Utilisation rapide :
    python app.py                    # écoute le micro, fenêtre toujours au-dessus
    python app.py --test q.wav       # traite un fichier audio au lieu du micro
    python app.py --autotest         # génère des wavs EN/FR avec espeak-ng et vérifie tout
    python app.py --telecharger      # télécharge UNE FOIS les modèles (seul moment en ligne)
    python app.py --lister-micros    # liste les périphériques d'entrée audio
    python app.py --creer-modele     # (re)crée un questions.md d'exemple
"""

from __future__ import annotations

import argparse
import importlib
import ipaddress
import json
import os
import platform
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import numpy as np

# ---------------------------------------------------------------------------
# Constantes et chemins
# ---------------------------------------------------------------------------

DOSSIER = Path(__file__).resolve().parent
FICHIER_CONFIG = DOSSIER / "config.json"
FICHIER_QUESTIONS = DOSSIER / "questions.md"
DOSSIER_TESTS = DOSSIER / "tests"
TAUX = 16000            # fréquence d'échantillonnage attendue par Whisper (Hz)
DUREE_BLOC_S = 0.1      # taille d'un bloc audio analysé par le découpeur (100 ms)
PY = f'"{sys.executable}"' if " " in sys.executable else sys.executable
_app = str(Path(__file__).resolve())
APP = f'"{_app}"' if " " in _app else _app      # chemin complet : commandes copiables depuis n'importe où

# Valeurs par défaut écrites dans config.json au premier lancement
CONFIG_DEFAUT = {
    "duree_silence_s": 0.7,          # silence (s) qui marque la fin d'une phrase
    "taille_modele": "small",        # "small" par défaut, "base" si le PC rame
    "type_calcul": "int8",           # compute_type de faster-whisper
    "seuil_correspondance": 60,      # score rapidfuzz minimal (0-100) pour afficher une réponse
    "peripherique_audio": None,      # null = micro par défaut ; sinon index ou nom (voir --lister-micros)
    "seuil_energie": 0.01,           # niveau RMS minimal considéré comme de la parole
    "duree_min_phrase_s": 0.4,       # en dessous : bruit ignoré
    "duree_max_phrase_s": 20,        # au-delà : la phrase est coupée et traitée
    "mots_min_correspondance": 2,    # « Okay. », « Great. » ne déclenchent pas de recherche
    "beam_size": 1,                  # 1 = le plus rapide ; 5 = un peu plus précis mais plus lent
    "threads_cpu": 0,                # 0 = automatique
    "taille_police_reponse": 26,
}

AIDE_CONFIG = {
    "duree_silence_s": "Durée de silence (secondes) qui marque la fin d'une phrase.",
    "taille_modele": "Modèle Whisper : 'small' (défaut, précis) ou 'base' (plus rapide si le PC rame).",
    "seuil_correspondance": "Score minimal 0-100 pour afficher une réponse (rapidfuzz token_set_ratio).",
    "peripherique_audio": "null = micro par défaut. Sinon index ou partie du nom : voir `python app.py --lister-micros`.",
    "seuil_energie": "Sensibilité du micro : baisser (ex. 0.005) si ta voix n'est pas détectée, monter si le bruit déclenche.",
}

# Modèle de base de questions (créé par --creer-modele, utilisé aussi par --autotest)
MODELE_QUESTIONS = """\
# interview-coach — base personnelle de questions / réponses
#
# Format (UTF-8, une entrée par ligne) :
#   question en anglais || réponse en anglais || réponse en français (optionnelle)
#
# - La question et la réponse principale sont TOUJOURS en anglais.
# - 3e colonne absente : si l'audio est en français, la réponse anglaise est
#   traduite automatiquement (Argos, en local).
# - Lignes vides et lignes commençant par # ignorées.
# - Garde des réponses courtes (2-4 phrases) : elles s'affichent en très grand.
#
# Exemples à adapter / compléter :

Tell me about yourself. || I'm a SKEMA MSc graduate in Finance, currently in Gas Origination and Structuring at ENGIE Global Markets. Before that I worked on counterparty risk and repo P&L at BNP Paribas CIB, with Python and VBA every day. I'm now looking for an analyst role in commodity markets from January 2027. || Je suis diplômé du MSc de SKEMA, majeure Finance, et je suis actuellement en Gas Origination & Structuring chez ENGIE Global Markets. Avant, j'ai travaillé sur le risque de contrepartie et le P&L repo chez BNP Paribas CIB, avec Python et VBA au quotidien. Je cherche un poste d'analyste sur les marchés de commodities à partir de janvier 2027.
Why do you want to work in commodities? || Commodities sit where macro, physical flows and risk management meet. At ENGIE I saw how a structured gas deal depends on storage, transport and weather, and I want to keep building that market intuition on a trading or structuring desk.
Why should we hire you? || I combine a market-risk background from BNP Paribas with hands-on origination experience at ENGIE. I am quick with data in Python, I understand how deals are priced and hedged, and I am ready to contribute from day one.
"""

# Phrases utilisées par --autotest : (fichier, voix espeak, texte, langue attendue, question attendue)
CAS_AUTOTEST = [
    ("question_en.wav", "en-us", "Why should we hire you?", "en", "Why should we hire you?"),
    ("question_fr.wav", "fr", "Pourquoi devrions-nous vous embaucher ?", "fr", "Why should we hire you?"),
    ("question_fr_colonne.wav", "fr", "Parlez-moi de vous.", "fr", "Tell me about yourself."),
]


class ErreurUtilisateur(Exception):
    """Erreur attendue : on affiche un message clair (avec la commande exacte) et on sort."""


def journal(msg: str) -> None:
    """Affiche une ligne horodatée dans la console."""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Garde réseau : aucune connexion sortante après le téléchargement des modèles
# ---------------------------------------------------------------------------

def _est_local(hote) -> bool:
    if hote is None:
        return True
    if isinstance(hote, bytes):
        hote = hote.decode(errors="ignore")
    hote = str(hote).strip("[]")
    if hote in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(hote.split("%")[0]).is_loopback
    except ValueError:
        return False


def bloquer_reseau() -> None:
    """Coupe toute connexion réseau non locale dans ce processus (contrainte 100 % local).

    - Mode hors ligne forcé pour Hugging Face (modèles Whisper lus uniquement depuis le cache).
    - Toute tentative de connexion / résolution DNS vers l'extérieur lève une erreur explicite.
    """
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"

    connect_orig = socket.socket.connect
    connect_ex_orig = socket.socket.connect_ex
    getaddrinfo_orig = socket.getaddrinfo

    def _refus(cible):
        return ConnectionRefusedError(
            f"[interview-coach] Connexion réseau bloquée vers {cible!r} (mode 100 % local). "
            f"Si un modèle manque, lance une fois : {PY} {APP} --telecharger"
        )

    def connect_garde(self, adresse):
        if isinstance(adresse, (str, bytes)) or _est_local(adresse[0]):   # socket Unix ou loopback
            return connect_orig(self, adresse)
        raise _refus(adresse)

    def connect_ex_garde(self, adresse):
        if isinstance(adresse, (str, bytes)) or _est_local(adresse[0]):
            return connect_ex_orig(self, adresse)
        raise _refus(adresse)

    def getaddrinfo_garde(hote, *args, **kwargs):
        if _est_local(hote):
            return getaddrinfo_orig(hote, *args, **kwargs)
        raise socket.gaierror(f"[interview-coach] Résolution DNS bloquée pour {hote!r} (mode 100 % local).")

    socket.socket.connect = connect_garde
    socket.socket.connect_ex = connect_ex_garde
    socket.getaddrinfo = getaddrinfo_garde


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def charger_config(chemin: Path = FICHIER_CONFIG) -> dict:
    """Crée config.json avec les valeurs par défaut s'il n'existe pas, puis le relit."""
    if not chemin.exists():
        contenu = dict(CONFIG_DEFAUT)
        contenu["_aide"] = AIDE_CONFIG
        chemin.write_text(json.dumps(contenu, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        journal(f"config.json créé avec les valeurs par défaut : {chemin}")
    try:
        lu = json.loads(chemin.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        raise ErreurUtilisateur(
            f"config.json est invalide (ligne {e.lineno}, colonne {e.colno} : {e.msg}).\n"
            f"Corrige-le, ou supprime-le pour qu'il soit recréé avec les valeurs par défaut :\n"
            f"   {chemin}"
        )
    cfg = dict(CONFIG_DEFAUT)
    cfg.update({k: v for k, v in lu.items() if not k.startswith("_")})

    # Contrôles simples des valeurs
    def nombre(cle, mini, maxi):
        v = cfg[cle]
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not (mini <= v <= maxi):
            raise ErreurUtilisateur(f"config.json : '{cle}' doit être un nombre entre {mini} et {maxi} (valeur lue : {v!r}).")

    nombre("duree_silence_s", 0.1, 5)
    nombre("seuil_correspondance", 0, 100)
    nombre("seuil_energie", 0.0, 1.0)
    nombre("duree_min_phrase_s", 0.0, 5)
    nombre("duree_max_phrase_s", 1, 120)
    nombre("beam_size", 1, 10)
    nombre("taille_police_reponse", 8, 80)
    if not isinstance(cfg["taille_modele"], str) or not cfg["taille_modele"]:
        raise ErreurUtilisateur("config.json : 'taille_modele' doit être un texte, ex. \"small\" ou \"base\".")
    return cfg


# ---------------------------------------------------------------------------
# Base de questions
# ---------------------------------------------------------------------------

@dataclass
class Entree:
    question: str
    reponse_en: str
    reponse_fr: Optional[str]
    ligne: int


def analyser_questions(texte: str, origine: str) -> list[Entree]:
    """Lit le format « question || réponse EN || réponse FR (optionnelle) »."""
    entrees: list[Entree] = []
    for num, brute in enumerate(texte.splitlines(), start=1):
        ligne = brute.strip()
        if not ligne or ligne.startswith("#"):
            continue
        if ligne[:2] in ("- ", "* "):            # tolère une puce markdown en début de ligne
            ligne = ligne[2:].strip()
        morceaux = [m.strip() for m in ligne.split("||")]
        if len(morceaux) < 2 or not morceaux[0] or not morceaux[1]:
            journal(f"⚠ {origine}, ligne {num} ignorée (format attendu : question || réponse EN || réponse FR optionnelle)")
            continue
        if len(morceaux) > 3:
            journal(f"⚠ {origine}, ligne {num} : plus de 3 colonnes, les colonnes en trop sont ignorées")
        fr = morceaux[2] if len(morceaux) >= 3 and morceaux[2] else None
        entrees.append(Entree(morceaux[0], morceaux[1], fr, num))
    return entrees


def charger_questions(chemin: Path = FICHIER_QUESTIONS) -> list[Entree]:
    if not chemin.exists():
        raise ErreurUtilisateur(
            f"questions.md introuvable : {chemin}\n"
            f"Il doit être à côté de app.py. Pour créer un modèle à compléter :\n"
            f"   {PY} {APP} --creer-modele"
        )
    entrees = analyser_questions(chemin.read_text(encoding="utf-8-sig"), "questions.md")
    if not entrees:
        raise ErreurUtilisateur(
            f"questions.md ne contient aucune entrée valide : {chemin}\n"
            f"Format d'une ligne :  question en anglais || réponse en anglais || réponse en français (optionnelle)"
        )
    return entrees


# ---------------------------------------------------------------------------
# Dépendances
# ---------------------------------------------------------------------------

def verifier_dependances(micro: bool) -> None:
    """Vérifie les paquets Python et donne la commande pip exacte s'il en manque."""
    paquets = [("faster_whisper", "faster-whisper"), ("argostranslate", "argostranslate"), ("rapidfuzz", "rapidfuzz")]
    if micro:
        paquets.append(("sounddevice", "sounddevice"))
    manquants = []
    for module, paquet in paquets:
        try:
            importlib.import_module(module)
        except ImportError:
            manquants.append(paquet)
        except OSError as e:   # sounddevice sans bibliothèque PortAudio (Linux)
            raise ErreurUtilisateur(
                f"sounddevice ne trouve pas PortAudio ({e}).\n"
                f"Sous Linux :  sudo apt install libportaudio2\n"
                f"Sous macOS :  brew install portaudio"
            )
    if manquants:
        raise ErreurUtilisateur(
            "Paquets Python manquants : " + ", ".join(manquants) + "\nInstalle-les avec :\n"
            f"   {PY} -m pip install " + " ".join(manquants)
        )


def message_tkinter_manquant() -> str:
    systeme = platform.system()
    if systeme == "Linux":
        cmd = "sudo apt install python3-tk"
    elif systeme == "Darwin":
        cmd = "brew install python-tk   (ou installe Python depuis python.org, Tkinter inclus)"
    else:
        cmd = "réinstalle Python depuis python.org en cochant « tcl/tk and IDLE »"
    return f"Tkinter (interface graphique) est absent.\n   {cmd}\nEn attendant : ajoute --sans-fenetre pour un affichage console."


# ---------------------------------------------------------------------------
# Traduction (Argos, hors ligne)
# ---------------------------------------------------------------------------

COMMANDES_ARGOS = (
    f"   {PY} {APP} --telecharger\n"
    "ou, à la main :\n"
    "   argospm update\n"
    "   argospm install translate-en_fr\n"
    "   argospm install translate-fr_en"
)


def stanza_hors_ligne() -> None:
    """Empêche stanza (découpage en phrases d'Argos) de retélécharger son index à chaque lancement.

    Par défaut, stanza.Pipeline va chercher resources.json sur GitHub à chaque création.
    Les fichiers sont déjà sur le disque depuis --telecharger : on force download_method=None.
    """
    try:
        from stanza.pipeline import core
    except Exception:
        return                                   # version d'Argos sans stanza : rien à faire
    init_orig = core.Pipeline.__init__
    if getattr(init_orig, "_hors_ligne", False):
        return

    def init_hors_ligne(self, *args, **kwargs):
        kwargs["download_method"] = None
        return init_orig(self, *args, **kwargs)

    init_hors_ligne._hors_ligne = True
    core.Pipeline.__init__ = init_hors_ligne


class Traducteur:
    """Traductions FR→EN (pour la recherche) et EN→FR (pour l'affichage), avec cache."""

    def __init__(self):
        stanza_hors_ligne()
        import argostranslate.translate as at
        langues = {l.code: l for l in at.get_installed_languages()}
        en, fr = langues.get("en"), langues.get("fr")
        self.fr_en = fr.get_translation(en) if (fr and en) else None
        self.en_fr = en.get_translation(fr) if (fr and en) else None
        manquants = [n for n, t in (("fr_en", self.fr_en), ("en_fr", self.en_fr)) if t is None]
        if manquants:
            raise ErreurUtilisateur(
                "Modèle(s) de traduction Argos manquant(s) : " + ", ".join(manquants) + "\n"
                "Télécharge-les une fois (connexion Internet nécessaire) :\n" + COMMANDES_ARGOS
            )
        self._cache: dict[tuple[str, str], str] = {}
        # un verrou par sens : la pré-traduction EN→FR ne bloque pas la traduction FR→EN des questions
        self._verrous = {"fr_en": threading.Lock(), "en_fr": threading.Lock()}

    def _traduire(self, sens: str, texte: str) -> str:
        cle = (sens, texte)
        with self._verrous[sens]:
            if cle not in self._cache:
                moteur = self.fr_en if sens == "fr_en" else self.en_fr
                self._cache[cle] = moteur.translate(texte).strip()
            return self._cache[cle]

    def fr_vers_en(self, texte: str) -> str:
        return self._traduire("fr_en", texte)

    def en_vers_fr(self, texte: str) -> str:
        return self._traduire("en_fr", texte)


# ---------------------------------------------------------------------------
# Transcription (faster-whisper, hors ligne)
# ---------------------------------------------------------------------------

class Transcripteur:
    def __init__(self, cfg: dict):
        from faster_whisper import WhisperModel
        self.cfg = cfg
        taille = cfg["taille_modele"]
        try:
            self.modele = WhisperModel(
                taille, device="cpu", compute_type=cfg["type_calcul"],
                cpu_threads=int(cfg["threads_cpu"]), local_files_only=True,
            )
        except Exception as e:
            raise ErreurUtilisateur(
                f"Modèle Whisper « {taille} » introuvable en local ({type(e).__name__}: {e}).\n"
                f"Télécharge-le une fois (connexion Internet nécessaire) :\n"
                f"   {PY} {APP} --telecharger\n"
                f"ou, à la main :\n"
                f"   {PY} -c \"from faster_whisper import download_model; download_model('{taille}')\""
            )

    def _passe(self, audio: np.ndarray, langue: Optional[str]):
        segments, info = self.modele.transcribe(
            audio, language=langue, vad_filter=True, beam_size=int(self.cfg["beam_size"]),
            condition_on_previous_text=False, without_timestamps=True,
        )
        texte = " ".join(s.text.strip() for s in segments).strip()   # le décodage a lieu ici
        return texte, info

    def transcrire(self, audio: np.ndarray) -> tuple[str, str, float]:
        """Renvoie (texte, langue 'en'|'fr', probabilité de la langue). Langue NON imposée."""
        texte, info = self._passe(audio, None)
        langue, proba = info.language, float(info.language_probability or 0.0)
        if langue not in ("en", "fr"):
            # Whisper a hésité vers une autre langue : on garde la plus probable entre EN et FR
            probas = dict(info.all_language_probs or [])
            langue = "fr" if probas.get("fr", 0.0) > probas.get("en", 0.0) else "en"
            proba = float(probas.get(langue, 0.0))
            texte, _ = self._passe(audio, langue)
        return texte, langue, proba

    def prechauffer(self) -> None:
        """Premier passage à vide pour ne pas payer l'initialisation sur la 1re question."""
        bruit = (np.random.default_rng(0).standard_normal(TAUX) * 1e-3).astype(np.float32)
        segs, _ = self.modele.transcribe(bruit, language="en", vad_filter=False, beam_size=1, without_timestamps=True)
        for _ in segs:
            pass


# ---------------------------------------------------------------------------
# Correspondance (rapidfuzz, toujours en anglais)
# ---------------------------------------------------------------------------

class Correspondance:
    def __init__(self, entrees: list[Entree]):
        from rapidfuzz import fuzz, utils
        self.fuzz, self.utils = fuzz, utils
        self.entrees = entrees
        self.questions = [utils.default_process(e.question) for e in entrees]

    def chercher(self, requete_en: str) -> tuple[Optional[int], float]:
        """Meilleure entrée par token_set_ratio ; à égalité, départage par ratio simple."""
        req = self.utils.default_process(requete_en)
        if not req:
            return None, 0.0
        meilleur, cle_max = None, (-1.0, -1.0)
        for i, q in enumerate(self.questions):
            cle = (self.fuzz.token_set_ratio(req, q), self.fuzz.ratio(req, q))
            if cle > cle_max:
                meilleur, cle_max = i, cle
        return meilleur, float(cle_max[0])


# ---------------------------------------------------------------------------
# Pipeline complet
# ---------------------------------------------------------------------------

@dataclass
class Resultat:
    texte: str                       # phrase entendue, langue d'origine
    langue: str                      # 'en' ou 'fr'
    proba_langue: float
    requete_en: str                  # texte comparé à la base (traduit si audio FR)
    entree: Optional[Entree]
    score: float
    meilleure_question: Optional[str]
    reponse: Optional[str]           # réponse dans la langue de l'audio (None si pas de correspondance)
    source_reponse: str
    duree_audio: float
    t_transcription: float = 0.0
    t_trad_requete: float = 0.0
    t_match: float = 0.0
    t_trad_reponse: float = 0.0
    t_total: float = 0.0             # temps de traitement après la fin de la phrase
    nom_fichier: str = ""


class Moteur:
    def __init__(self, cfg: dict, entrees: list[Entree]):
        self.cfg = cfg
        self.entrees = entrees
        t0 = time.perf_counter()
        journal(f"Chargement de Whisper « {cfg['taille_modele']} » ({cfg['type_calcul']}, CPU)…")
        self.transcripteur = Transcripteur(cfg)
        journal("Chargement des modèles de traduction Argos (fr_en, en_fr)…")
        self.traducteur = Traducteur()
        self.correspondance = Correspondance(entrees)
        journal("Préchauffage…")
        self.transcripteur.prechauffer()
        try:
            self.traducteur.fr_vers_en("Bonjour.")
            self.traducteur.en_vers_fr("Hello.")
        except Exception as e:
            raise ErreurUtilisateur(
                f"La traduction Argos n'a pas pu démarrer hors ligne ({type(e).__name__}: {str(e)[:200]}).\n"
                f"Il manque probablement des fichiers : relance une fois, avec Internet :\n"
                f"   {PY} {APP} --telecharger"
            )
        journal(f"Prêt en {time.perf_counter() - t0:.1f} s — {len(entrees)} question(s) dans la base.")

    def pretraduire(self) -> None:
        """Traduit à l'avance en FR les réponses sans 3e colonne (latence plus faible ensuite)."""
        a_faire = [e for e in self.entrees if not e.reponse_fr]
        t0 = time.perf_counter()
        for e in a_faire:
            self.traducteur.en_vers_fr(e.reponse_en)
        if a_faire:
            journal(f"{len(a_faire)} réponse(s) pré-traduite(s) en français en {time.perf_counter() - t0:.1f} s.")

    def traiter(self, audio: np.ndarray, t_fin_phrase: float) -> Optional[Resultat]:
        """Transcription → (traduction FR→EN) → correspondance → réponse dans la langue de l'audio."""
        t = time.perf_counter()
        texte, langue, proba = self.transcripteur.transcrire(audio)
        t_transcription = time.perf_counter() - t
        if not texte:
            return None

        t = time.perf_counter()
        requete_en = texte if langue == "en" else self.traducteur.fr_vers_en(texte)
        t_trad_requete = time.perf_counter() - t

        t = time.perf_counter()
        idx, score = (None, 0.0)
        if len(requete_en.split()) >= int(self.cfg["mots_min_correspondance"]):
            idx, score = self.correspondance.chercher(requete_en)
        t_match = time.perf_counter() - t

        entree = self.entrees[idx] if idx is not None else None
        reponse, source, t_trad_reponse = None, "aucune correspondance", 0.0
        if entree is not None and score >= float(self.cfg["seuil_correspondance"]):
            if langue == "en":
                reponse, source = entree.reponse_en, "colonne EN"
            elif entree.reponse_fr:
                reponse, source = entree.reponse_fr, "colonne FR"
            else:
                t = time.perf_counter()
                reponse, source = self.traducteur.en_vers_fr(entree.reponse_en), "traduction Argos EN→FR"
                t_trad_reponse = time.perf_counter() - t

        return Resultat(
            texte=texte, langue=langue, proba_langue=proba, requete_en=requete_en,
            entree=entree if reponse else None, score=score,
            meilleure_question=entree.question if entree else None,
            reponse=reponse, source_reponse=source, duree_audio=len(audio) / TAUX,
            t_transcription=t_transcription, t_trad_requete=t_trad_requete, t_match=t_match,
            t_trad_reponse=t_trad_reponse, t_total=time.perf_counter() - t_fin_phrase,
        )


# ---------------------------------------------------------------------------
# Découpage en phrases (fin de phrase = silence ≥ duree_silence_s)
# ---------------------------------------------------------------------------

class Decoupeur:
    """Reçoit des blocs de 100 ms à 16 kHz et renvoie une phrase complète après un silence."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.preroll: deque = deque(maxlen=3)      # 300 ms gardés avant le début de la parole
        self.bruit = cfg["seuil_energie"] / 3      # estimation adaptative du bruit de fond
        self._reinitialiser()

    def _reinitialiser(self):
        self.en_parole = False
        self.tampon: list[np.ndarray] = []
        self.silence = 0.0
        self.parole = 0.0

    def seuil(self) -> float:
        return max(float(self.cfg["seuil_energie"]), 2.5 * self.bruit)

    def ajouter(self, bloc: np.ndarray) -> Optional[tuple[np.ndarray, float]]:
        """Renvoie (audio de la phrase, instant de fin détectée) ou None."""
        duree = len(bloc) / TAUX
        rms = float(np.sqrt(np.mean(np.square(bloc)))) if len(bloc) else 0.0
        parle = rms > self.seuil()
        if not self.en_parole:
            if parle:
                self.en_parole = True
                self.tampon = list(self.preroll) + [bloc]
                self.silence, self.parole = 0.0, duree
            else:
                self.bruit = 0.9 * self.bruit + 0.1 * rms
                self.preroll.append(bloc)
            return None

        self.tampon.append(bloc)
        if parle:
            self.silence = 0.0
            self.parole += duree
        else:
            self.silence += duree
        total = sum(len(b) for b in self.tampon) / TAUX
        if self.silence >= float(self.cfg["duree_silence_s"]) or total >= float(self.cfg["duree_max_phrase_s"]):
            audio = np.concatenate(self.tampon).astype(np.float32)
            assez = self.parole >= float(self.cfg["duree_min_phrase_s"])
            self._reinitialiser()
            self.preroll.clear()
            return (audio, time.perf_counter()) if assez else None
        return None

    def vider(self) -> Optional[tuple[np.ndarray, float]]:
        """Fin de fichier : renvoie la phrase en cours éventuelle."""
        if self.en_parole and self.parole >= float(self.cfg["duree_min_phrase_s"]):
            audio = np.concatenate(self.tampon).astype(np.float32)
            self._reinitialiser()
            return audio, time.perf_counter()
        self._reinitialiser()
        return None


# ---------------------------------------------------------------------------
# Affichage console
# ---------------------------------------------------------------------------

def ligne_statut(r: Resultat) -> str:
    detail = f"transcr. {r.t_transcription:.2f}"
    if r.langue == "fr":
        detail += f" · trad. FR→EN {r.t_trad_requete:.2f}"
    if r.t_trad_reponse:
        detail += f" · trad. EN→FR {r.t_trad_reponse:.2f}"
    cible = "OK" if r.t_total < 2.0 else "> 2 s"
    source = r.source_reponse
    if not r.reponse and r.meilleure_question:
        source += f" (plus proche : « {r.meilleure_question} », {r.score:.0f})"
    return (f"Langue : {r.langue.upper()} ({r.proba_langue:.0%}) · score {r.score:.0f} · "
            f"traitement {r.t_total:.2f} s [{cible}] ({detail}) · {source}")


def afficher_console(r: Resultat) -> None:
    print("-" * 78)
    print(f"Entendu ({r.langue.upper()}) : {r.texte}")
    if r.langue == "fr":
        print(f"Traduit EN       : {r.requete_en}")
    if r.reponse:
        print(f"Question         : {r.entree.question}")
        print(f"RÉPONSE ({r.langue.upper()})    : {r.reponse}")
    else:
        proche = f" (le plus proche : « {r.meilleure_question} », {r.score:.0f})" if r.meilleure_question else ""
        print(f"Pas de réponse au-dessus du seuil{proche}")
    print(ligne_statut(r))
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Fenêtre Tkinter (toujours au-dessus)
# ---------------------------------------------------------------------------

class Fenetre:
    COULEURS = {"fond": "#16181d", "phrase": "#c8ccd4", "reponse": "#ffffff", "pas_rep": "#8a909c",
                "histo": "#5f6570", "statut": "#8a909c"}

    def __init__(self, cfg: dict, titre_mode: str):
        import tkinter as tk
        self.tk = tk
        self.cfg = cfg
        self.file: queue.Queue = queue.Queue()
        self.historique: deque = deque(maxlen=3)
        famille = {"Windows": "Segoe UI", "Darwin": "Helvetica Neue"}.get(platform.system(), "DejaVu Sans")
        self.famille = famille
        c = self.COULEURS

        self.racine = tk.Tk()
        self.racine.title(f"interview-coach — {titre_mode}")
        self.racine.configure(bg=c["fond"])
        self.racine.attributes("-topmost", True)
        self.racine.geometry("1000x460+40+40")
        self.racine.minsize(500, 260)
        self.racine.bind("<Escape>", lambda _e: self.racine.destroy())

        opts = dict(bg=c["fond"], anchor="w", justify="left")
        self.lbl_phrase = tk.Label(self.racine, text="En attente d'une question…", fg=c["phrase"],
                                   font=(famille, 15), **opts)
        self.lbl_reponse = tk.Label(self.racine, text="", fg=c["reponse"], anchor="nw", justify="left",
                                    bg=c["fond"], font=(famille, int(cfg["taille_police_reponse"]), "bold"))
        self.lbl_histo = tk.Label(self.racine, text="Historique : —", fg=c["histo"], font=(famille, 9), **opts)
        self.lbl_statut = tk.Label(self.racine, text="Démarrage…", fg=c["statut"], font=(famille, 10), **opts)

        self.lbl_phrase.pack(fill="x", padx=18, pady=(14, 4))
        self.lbl_reponse.pack(fill="both", expand=True, padx=18, pady=4)
        self.lbl_histo.pack(fill="x", padx=18, pady=(2, 0))
        self.lbl_statut.pack(fill="x", padx=18, pady=(0, 10))
        self.racine.bind("<Configure>", self._ajuster_largeur)
        self.racine.after(50, self._depiler)

    def _ajuster_largeur(self, _e=None):
        largeur = max(200, self.racine.winfo_width() - 40)
        for lbl in (self.lbl_phrase, self.lbl_reponse, self.lbl_histo, self.lbl_statut):
            lbl.configure(wraplength=largeur)

    def _taille_reponse(self, texte: str) -> int:
        """Police très grande, réduite automatiquement pour les réponses longues."""
        base = int(self.cfg["taille_police_reponse"])
        n = len(texte)
        if n > 450:
            return max(12, int(base * 0.6))
        if n > 250:
            return max(12, int(base * 0.78))
        return base

    # Appels possibles depuis n'importe quel thread
    def publier(self, r: Resultat) -> None:
        self.file.put(("resultat", r))

    def etat(self, texte: str) -> None:
        self.file.put(("etat", texte))

    def _depiler(self):
        try:
            while True:
                genre, val = self.file.get_nowait()
                if genre == "etat":
                    self.lbl_statut.configure(text=val)
                else:
                    self._afficher(val)
        except queue.Empty:
            pass
        self.racine.after(50, self._depiler)

    def _afficher(self, r: Resultat):
        c = self.COULEURS
        prefixe = r.nom_fichier + " · " if r.nom_fichier else ""
        if r.reponse:
            self.lbl_phrase.configure(text=f"{prefixe}« {r.texte} »")
            self.lbl_reponse.configure(text=r.reponse, fg=c["reponse"],
                                       font=(self.famille, self._taille_reponse(r.reponse), "bold"))
            self.historique.appendleft(f"[{r.langue.upper()} {r.score:.0f}] {r.entree.question}")
            self.lbl_histo.configure(text="Historique : " + "   ·   ".join(self.historique))
        else:
            # Pas de correspondance : seule la transcription (et sa traduction si FR) est affichée.
            # La réponse précédente reste visible mais grisée, pour qu'un « Okay » ne l'efface pas.
            traduction = f"   →  EN : {r.requete_en}" if r.langue == "fr" else ""
            self.lbl_phrase.configure(text=f"{prefixe}« {r.texte} »{traduction}")
            self.lbl_reponse.configure(fg=c["pas_rep"])
        self.lbl_statut.configure(text=ligne_statut(r))

    def boucle(self):
        self._ajuster_largeur()
        self.racine.mainloop()


# ---------------------------------------------------------------------------
# Sources audio : micro et fichier
# ---------------------------------------------------------------------------

def lister_micros() -> None:
    verifier_dependances(micro=True)
    import sounddevice as sd
    try:
        defaut = sd.default.device[0]          # paire (entrée, sortie)
    except (TypeError, IndexError):
        defaut = sd.default.device
    print("Périphériques d'entrée audio (mets l'index ou le nom dans config.json → \"peripherique_audio\") :")
    trouves = 0
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            trouves += 1
            marque = "  ← défaut" if i == defaut else ""
            print(f"  [{i}] {d['name']}  ({int(d['default_samplerate'])} Hz){marque}")
    if not trouves:
        print("  (aucun)")


def ouvrir_micro(cfg: dict, rappel_bloc: Callable[[np.ndarray], None]):
    """Ouvre le micro en 16 kHz mono ; si le périphérique refuse le 16 kHz, rééchantillonne."""
    import sounddevice as sd
    aide = (f"Vérifie qu'un micro est branché et autorisé (Windows : Paramètres > Confidentialité > Microphone ;"
            f" macOS : Réglages > Confidentialité > Micro pour ton terminal).\n"
            f"Liste des micros : {PY} {APP} --lister-micros  puis renseigne \"peripherique_audio\" dans config.json.")
    try:
        entrees = [d for d in sd.query_devices() if d["max_input_channels"] > 0]
    except Exception as e:
        raise ErreurUtilisateur(f"Impossible d'interroger les périphériques audio ({e}).\n{aide}")
    if not entrees:
        raise ErreurUtilisateur(f"Aucun micro accessible.\n{aide}")

    dev = cfg["peripherique_audio"]
    taux = TAUX
    try:
        sd.check_input_settings(device=dev, samplerate=TAUX, channels=1, dtype="float32")
    except Exception:
        try:
            taux = int(sd.query_devices(dev, "input")["default_samplerate"])
            sd.check_input_settings(device=dev, samplerate=taux, channels=1, dtype="float32")
            journal(f"Le micro ne gère pas 16 kHz : capture à {taux} Hz puis rééchantillonnage.")
        except Exception as e:
            raise ErreurUtilisateur(f"Micro inaccessible (périphérique {dev!r}) : {e}\n{aide}")

    taille_bloc = int(taux * DUREE_BLOC_S)
    x_src = np.linspace(0, 1, taille_bloc, endpoint=False)
    x_dst = np.linspace(0, 1, int(TAUX * DUREE_BLOC_S), endpoint=False)

    def rappel(indata, _frames, _t, statut):
        bloc = indata[:, 0].astype(np.float32, copy=True)
        if taux != TAUX:
            bloc = np.interp(x_dst, x_src, bloc).astype(np.float32)
        rappel_bloc(bloc)

    try:
        flux = sd.InputStream(samplerate=taux, channels=1, dtype="float32", blocksize=taille_bloc,
                              device=dev, callback=rappel)
        flux.start()
    except Exception as e:
        raise ErreurUtilisateur(f"Impossible d'ouvrir le micro ({e}).\n{aide}")
    nom = sd.query_devices(dev, "input")["name"]
    journal(f"Micro ouvert : {nom}")
    return flux


def lire_audio(chemin: Path) -> np.ndarray:
    """Lit un fichier audio quelconque en 16 kHz mono float32."""
    if not chemin.exists():
        raise ErreurUtilisateur(f"Fichier audio introuvable : {chemin}")
    from faster_whisper import decode_audio
    try:
        return decode_audio(str(chemin), sampling_rate=TAUX).astype(np.float32)
    except Exception as e:
        raise ErreurUtilisateur(f"Impossible de lire {chemin} ({e}).")


def traiter_fichier(moteur: Moteur, cfg: dict, chemin: Path,
                    sortie: Optional[Callable[[Resultat], None]] = None) -> list[Resultat]:
    """Mode test : rejoue le fichier dans le même découpeur que le micro, phrase par phrase.

    Chaque phrase est traitée dès que sa fin est détectée, comme en direct : le temps
    affiché est donc bien le temps de traitement après la fin de la phrase.
    """
    audio = lire_audio(chemin)
    audio = np.concatenate([np.zeros(int(0.3 * TAUX), np.float32), audio,
                            np.zeros(int((cfg["duree_silence_s"] + 0.5) * TAUX), np.float32)])
    decoupeur = Decoupeur(cfg)
    pas = int(TAUX * DUREE_BLOC_S)
    resultats: list[Resultat] = []

    def traiter(phrase):
        if phrase is None:
            return
        r = moteur.traiter(*phrase)
        if r is None:
            return
        r.nom_fichier = chemin.name
        resultats.append(r)
        if sortie:
            sortie(r)

    for i in range(0, len(audio), pas):
        traiter(decoupeur.ajouter(audio[i:i + pas]))
    traiter(decoupeur.vider())
    if not resultats:
        journal(f"⚠ {chemin.name} : aucune parole détectée (fichier silencieux ? baisse \"seuil_energie\").")
    return resultats


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

def mode_micro(cfg: dict, moteur: Moteur, fenetre: Optional[Fenetre]) -> None:
    file_audio: queue.Queue = queue.Queue()
    file_phrases: queue.Queue = queue.Queue()
    decoupeur = Decoupeur(cfg)
    flux = ouvrir_micro(cfg, file_audio.put)
    etat = fenetre.etat if fenetre else (lambda _t: None)
    etat("● Écoute… (Échap pour quitter)")

    def boucle_ecoute():
        parlait = False
        while True:
            phrase = decoupeur.ajouter(file_audio.get())
            if decoupeur.en_parole != parlait:
                parlait = decoupeur.en_parole
                if parlait:
                    etat("● Parole détectée…")
            if phrase:
                etat("⏳ Traitement…")
                file_phrases.put(phrase)

    def boucle_traitement():
        while True:
            audio, t_fin = file_phrases.get()
            try:
                r = moteur.traiter(audio, t_fin)
            except Exception as e:
                journal(f"Erreur de traitement : {e}")
                etat(f"Erreur : {e}")
                continue
            if r is None:
                etat("● Écoute…")
                continue
            afficher_console(r)
            if fenetre:
                fenetre.publier(r)

    threading.Thread(target=boucle_ecoute, daemon=True).start()
    threading.Thread(target=boucle_traitement, daemon=True).start()
    threading.Thread(target=moteur.pretraduire, daemon=True).start()
    try:
        if fenetre:
            fenetre.boucle()
        else:
            journal("Écoute en cours (Ctrl+C pour quitter)…")
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        flux.stop()
        flux.close()


def mode_test(cfg: dict, moteur: Moteur, fichiers: list[Path], fenetre: Optional[Fenetre]) -> list[Resultat]:
    moteur.pretraduire()
    tous: list[Resultat] = []

    def sortie(r):
        afficher_console(r)
        if fenetre:
            fenetre.publier(r)

    def executer():
        for f in fichiers:
            journal(f"Test : {f}")
            if fenetre:
                fenetre.etat(f"⏳ Traitement de {f.name}…")
            tous.extend(traiter_fichier(moteur, cfg, f, sortie))
        journal("Mode test terminé." + (" Ferme la fenêtre (ou Échap) pour quitter." if fenetre else ""))

    if fenetre:
        threading.Thread(target=executer, daemon=True).start()
        fenetre.boucle()
    else:
        executer()
    return tous


def trouver_espeak() -> Optional[str]:
    for nom in ("espeak-ng", "espeak"):
        chemin = shutil.which(nom)
        if chemin:
            return chemin
    for chemin in (r"C:\Program Files\eSpeak NG\espeak-ng.exe", r"C:\Program Files (x86)\eSpeak NG\espeak-ng.exe"):
        if Path(chemin).exists():
            return chemin
    return None


def mode_autotest(cfg: dict, avec_fenetre: bool) -> int:
    """Génère des wavs EN/FR avec espeak-ng et vérifie langue, correspondance, langue de la réponse, latence."""
    espeak = trouver_espeak()
    if not espeak:
        cmd = {"Windows": "winget install --id eSpeak-NG.eSpeak-NG -e   (puis rouvre le terminal)",
               "Darwin": "brew install espeak-ng"}.get(platform.system(), "sudo apt install espeak-ng")
        raise ErreurUtilisateur(f"espeak-ng introuvable : impossible de générer les wavs de test.\n   {cmd}\n"
                                f"Tu peux aussi tester avec ton propre enregistrement : {PY} {APP} --test ma_question.wav")
    DOSSIER_TESTS.mkdir(exist_ok=True)
    journal(f"Génération des wavs de test avec {espeak}")
    for fichier, voix, texte, *_ in CAS_AUTOTEST:
        chemin = DOSSIER_TESTS / fichier
        subprocess.run([espeak, "-v", voix, "-s", "140", "-w", str(chemin), texte], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        journal(f"  {chemin.name} ← « {texte} » ({voix})")

    # L'autotest utilise la base d'exemple intégrée, pour ne pas dépendre de tes modifications
    entrees = analyser_questions(MODELE_QUESTIONS, "base d'exemple")
    moteur = Moteur(cfg, entrees)
    moteur.pretraduire()
    fenetre = None
    if avec_fenetre:
        fenetre = Fenetre(cfg, "autotest")

    bilan: list[tuple[str, bool, str]] = []

    def executer():
        for fichier, _voix, texte, langue_att, question_att in CAS_AUTOTEST:
            res = traiter_fichier(moteur, cfg, DOSSIER_TESTS / fichier,
                                  lambda r: (afficher_console(r), fenetre.publier(r) if fenetre else None))
            if not res:
                bilan.append((fichier, False, "aucune parole détectée"))
                continue
            r = max(res, key=lambda x: x.score)
            entree = next(e for e in entrees if e.question == question_att)
            if langue_att == "en":
                attendu, src_att = entree.reponse_en, "colonne EN"
            elif entree.reponse_fr:
                attendu, src_att = entree.reponse_fr, "colonne FR"
            else:
                attendu, src_att = moteur.traducteur.en_vers_fr(entree.reponse_en), "traduction Argos EN→FR"
            problemes = []
            if r.langue != langue_att:
                problemes.append(f"langue détectée {r.langue} au lieu de {langue_att}")
            if r.entree is None or r.entree.question != question_att:
                problemes.append(f"question trouvée « {r.meilleure_question} » ({r.score:.0f}) au lieu de « {question_att} »")
            elif r.reponse != attendu or r.source_reponse != src_att:
                problemes.append(f"réponse affichée inattendue (source : {r.source_reponse}, attendu : {src_att})")
            detail = (f"langue {r.langue.upper()} · score {r.score:.0f} · réponse {r.langue.upper()} ({r.source_reponse})"
                      f" · traitement {r.t_total:.2f} s")
            if r.t_total >= 2.0:
                detail += "  ⚠ au-dessus de la cible de 2 s (essaie \"taille_modele\": \"base\")"
            bilan.append((fichier, not problemes, detail if not problemes else "; ".join(problemes)))

        print("\n" + "=" * 78 + "\nBILAN AUTOTEST")
        for fichier, ok, detail in bilan:
            print(f"  [{'OK ' if ok else 'ÉCHEC'}] {fichier:26s} {detail}")
        print("=" * 78, flush=True)
        if fenetre:
            fenetre.etat("Autotest : " + ("tout est OK" if all(b[1] for b in bilan) else "ÉCHEC — voir la console")
                         + " · Échap pour fermer")

    if fenetre:
        threading.Thread(target=executer, daemon=True).start()
        fenetre.boucle()
    else:
        executer()
    return 0 if bilan and all(ok for _, ok, _ in bilan) else 1


def telecharger(cfg: dict) -> None:
    """Seul moment où l'outil utilise le réseau : récupère Whisper et les deux paquets Argos."""
    verifier_dependances(micro=False)
    taille = cfg["taille_modele"]
    journal(f"Téléchargement du modèle Whisper « {taille} »…")
    from faster_whisper import download_model
    chemin = download_model(taille)
    journal(f"  OK → {chemin}")

    import argostranslate.package as ap
    import argostranslate.translate as at
    journal("Mise à jour de l'index Argos…")
    ap.update_package_index()
    dispo = ap.get_available_packages()
    installes = {(p.from_code, p.to_code) for p in ap.get_installed_packages()}
    for de, vers in (("en", "fr"), ("fr", "en")):
        if (de, vers) in installes:
            journal(f"  translate-{de}_{vers} déjà installé")
            continue
        paquet = next((p for p in dispo if p.from_code == de and p.to_code == vers), None)
        if paquet is None:
            raise ErreurUtilisateur(f"Paquet Argos translate-{de}_{vers} introuvable dans l'index.")
        journal(f"  Installation de translate-{de}_{vers}…")
        ap.install_from_path(paquet.download())
    # Traduction d'essai : déclenche ici, tant que le réseau est autorisé, tout fichier chargé à la demande
    journal("Essai de traduction : " + at.translate("Why should we hire you?", "en", "fr")
            + " | " + at.translate("Pourquoi devrions-nous vous embaucher ?", "fr", "en"))
    journal("Terminé. L'outil fonctionne désormais entièrement hors ligne.")


# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    for flux in (sys.stdout, sys.stderr):        # évite les plantages d'accents sur les consoles Windows
        try:
            flux.reconfigure(errors="replace")
        except Exception:
            pass

    p = argparse.ArgumentParser(description="interview-coach : réponses d'entretien suggérées en direct, 100 % local.")
    p.add_argument("--test", nargs="+", metavar="FICHIER.wav", help="traite un ou plusieurs fichiers audio au lieu du micro")
    p.add_argument("--autotest", action="store_true", help="génère des wavs EN/FR avec espeak-ng et valide le pipeline")
    p.add_argument("--telecharger", action="store_true", help="télécharge une fois Whisper + Argos en_fr et fr_en")
    p.add_argument("--lister-micros", action="store_true", help="liste les périphériques d'entrée audio")
    p.add_argument("--creer-modele", action="store_true", help="crée questions.md (exemples) s'il n'existe pas")
    p.add_argument("--sans-fenetre", action="store_true", help="affichage console uniquement")
    args = p.parse_args(argv)

    try:
        if args.creer_modele:
            if FICHIER_QUESTIONS.exists():
                journal(f"questions.md existe déjà, rien n'est écrasé : {FICHIER_QUESTIONS}")
            else:
                FICHIER_QUESTIONS.write_text(MODELE_QUESTIONS, encoding="utf-8")
                journal(f"Modèle créé : {FICHIER_QUESTIONS} — complète-le avec tes questions.")
            return 0

        cfg = charger_config()

        if args.telecharger:
            telecharger(cfg)
            return 0

        bloquer_reseau()                         # à partir d'ici : aucun appel réseau possible
        if args.lister_micros:
            lister_micros()
            return 0

        avec_fenetre = not args.sans_fenetre
        if avec_fenetre:
            try:
                import tkinter  # noqa: F401
            except ImportError:
                raise ErreurUtilisateur(message_tkinter_manquant())

        if args.autotest:
            verifier_dependances(micro=False)
            return mode_autotest(cfg, avec_fenetre)

        verifier_dependances(micro=not args.test)
        entrees = charger_questions()
        if args.test:
            fichiers = [Path(f).expanduser().resolve() for f in args.test]
            for f in fichiers:
                if not f.exists():
                    raise ErreurUtilisateur(f"Fichier audio introuvable : {f}")
            moteur = Moteur(cfg, entrees)
            fenetre = Fenetre(cfg, "mode test") if avec_fenetre else None
            mode_test(cfg, moteur, fichiers, fenetre)
            return 0

        moteur = Moteur(cfg, entrees)
        fenetre = Fenetre(cfg, "écoute du micro") if avec_fenetre else None
        mode_micro(cfg, moteur, fenetre)
        return 0

    except ErreurUtilisateur as e:
        print(f"\n[ERREUR] {e}\n", file=sys.stderr, flush=True)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
