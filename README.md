# interview-coach

Entraînement aux entretiens avec des recruteurs anglophones, **100 % local**.
L'outil écoute, transcrit la question (anglais ou français, détection automatique),
la rapproche de ta base `questions.md` (en anglais) et affiche ta réponse préparée
**dans la langue de l'audio** : question en anglais → réponse en anglais, question en
français → réponse en français.

```
micro / .wav ─► découpage (silence ≥ 0,7 s) ─► faster-whisper (langue auto)
   ─► [si FR : Argos fr→en] ─► rapidfuzz token_set_ratio vs questions anglaises
   ─► réponse : EN telle quelle | FR = 3e colonne, sinon Argos en→fr
```

Fichiers : `app.py` (tout le programme), `questions.md` (ta base), `config.json`
(créé au premier lancement), `tests/` (wavs générés par `--autotest`).

`questions.md` et `config.json` sont **exclus du dépôt git** (`.gitignore`) car ils contiennent des données
personnelles. Depuis un clone : `cp questions.example.md questions.md` (ou `python app.py --creer-modele`),
puis complète-le.

`questions.md` et `config.json` sont **exclus du dépôt git** (`.gitignore`) car ils contiennent des données
personnelles. Pour démarrer depuis un clone : `cp questions.example.md questions.md`
(ou `python app.py --creer-modele`), puis complète-le.

---

## 1. Installation

Je n'ai pas pu détecter ton système depuis ici : les commandes sont données pour
**Windows** (le plus probable), puis macOS et Linux. Python **3.11 ou 3.12** conseillé.
Toutes les commandes se lancent **dans le dossier `interview-coach`**.

### Windows (PowerShell)

1. Installer Python 3.12 (si `py --version` ne répond pas) :
   ```powershell
   winget install --id Python.Python.3.12 -e
   ```
2. Créer un environnement virtuel :
   ```powershell
   py -3.12 -m venv .venv
   ```
3. L'activer (si PowerShell refuse : `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, puis recommencer) :
   ```powershell
   .\.venv\Scripts\Activate.ps1
   ```
4. Mettre pip à jour :
   ```powershell
   python -m pip install --upgrade pip
   ```
5. Installer les paquets :
   ```powershell
   python -m pip install faster-whisper argostranslate rapidfuzz sounddevice
   ```
6. Télécharger les modèles (**seul moment où Internet est utilisé** : Whisper `small`,
   Argos `en_fr` **et** `fr_en`) :
   ```powershell
   python app.py --telecharger
   ```
7. (Pour l'autotest) installer espeak-ng, puis **rouvrir le terminal** :
   ```powershell
   winget install --id eSpeak-NG.eSpeak-NG -e
   ```

Tkinter est inclus avec Python de python.org / winget. S'il manque, réinstalle Python en
cochant « tcl/tk and IDLE ».

### macOS (Terminal)

```bash
brew install python@3.12 python-tk@3.12 portaudio espeak-ng
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install faster-whisper argostranslate rapidfuzz sounddevice
python app.py --telecharger
```
Au premier lancement, autorise l'accès au micro pour le Terminal (Réglages Système >
Confidentialité et sécurité > Micro).

### Linux (Ubuntu / Debian)

```bash
sudo apt install python3-venv python3-tk libportaudio2 espeak-ng
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install faster-whisper argostranslate rapidfuzz sounddevice
python app.py --telecharger
```
Si pip se met à télécharger une version CUDA de torch (plusieurs Go) comme dépendance
d'Argos, installe d'abord la version CPU :
`python -m pip install torch --index-url https://download.pytorch.org/whl/cpu`.

### Téléchargement manuel des modèles (alternative à `--telecharger`)

```bash
argospm update
argospm install translate-en_fr
argospm install translate-fr_en
python -c "from faster_whisper import download_model; download_model('small')"
```
(Pour le modèle `base` : remplace `'small'` par `'base'`.)

---

## 2. Utilisation

Active d'abord l'environnement (`.\.venv\Scripts\Activate.ps1` sous Windows,
`source .venv/bin/activate` ailleurs).

| Commande | Effet |
|---|---|
| `python app.py` | écoute le micro, fenêtre toujours au-dessus |
| `python app.py --test question.wav` | traite un fichier audio au lieu du micro (plusieurs fichiers possibles) |
| `python app.py --autotest` | génère 3 wavs (EN, FR, FR avec 3e colonne) avec espeak-ng et vérifie tout le pipeline |
| `python app.py --lister-micros` | liste les micros (index à mettre dans `config.json`) |
| `python app.py --creer-modele` | recrée un `questions.md` d'exemple s'il n'existe pas |
| `python app.py --telecharger` | télécharge les modèles (une fois) |
| ajouter `--sans-fenetre` | affichage console uniquement |

**La fenêtre** : en haut, la dernière phrase entendue (langue d'origine) ; au centre, en très
grand, la réponse suggérée (langue de l'audio) ; une ligne discrète avec les 3 dernières
correspondances ; en bas, le statut : langue détectée, score, **temps de traitement après la
fin de la phrase** (cible < 2 s). Si rien ne correspond, seule la transcription (et sa
traduction anglaise si l'audio est en français) s'affiche, et la réponse précédente reste
visible en grisé. `Échap` ferme la fenêtre.

**Capter la voix du recruteur en visio** : avec un casque, le micro n'entend que toi.
Choisis alors un périphérique de « boucle » dans `peripherique_audio` (Windows : *Mixage
stéréo* ou VB-CABLE ; macOS : BlackHole), visible avec `--lister-micros`.

---

## 3. La base `questions.md`

Une entrée par ligne, UTF-8 :

```
question en anglais || réponse en anglais || réponse en français (optionnelle)
```

- Lignes vides et lignes commençant par `#` ignorées.
- Sans 3e colonne, la réponse française est traduite à la volée par Argos (et pré-traduite
  au démarrage pour rester rapide). Écris la 3e colonne pour les réponses importantes :
  ta formulation sera meilleure qu'une traduction automatique.
- Réponses courtes (2-4 phrases) : elles s'affichent en très grand.
- Plusieurs formulations d'une même question = plusieurs lignes avec la même réponse.

---

## 4. `config.json`

Créé au premier lancement, relu à chaque démarrage.

| Clé | Défaut | Rôle |
|---|---|---|
| `duree_silence_s` | 0.7 | silence qui marque la fin d'une phrase |
| `taille_modele` | `"small"` | `"base"` si le PC rame (relancer `--telecharger` une fois) |
| `seuil_correspondance` | 60 | score minimal (0-100) pour afficher une réponse |
| `peripherique_audio` | `null` | micro par défaut ; sinon index ou partie du nom |
| `seuil_energie` | 0.01 | sensibilité : baisser si ta voix n'est pas détectée |
| `beam_size` | 1 | 1 = le plus rapide ; 5 = un peu plus précis |
| `mots_min_correspondance` | 2 | « Okay. » ou « Great. » ne déclenchent pas de recherche |
| `taille_police_reponse` | 26 | taille de la réponse (réduite automatiquement si longue) |

---

## 5. Confidentialité

Après `--telecharger`, l'outil fonctionne sans réseau : au démarrage il force le mode hors
ligne de Hugging Face et **bloque toute connexion réseau non locale dans son processus**.
Si un composant tentait d'en ouvrir une, tu verrais une erreur explicite au lieu d'un envoi
silencieux. Aucun audio ni texte n'est enregistré sur disque (hors les wavs de `--autotest`).

## 6. Dépannage

- *« Modèle Whisper introuvable »* / *« Modèle Argos manquant »* → `python app.py --telecharger`
- *« Aucun micro accessible »* → vérifier l'autorisation micro du système, puis `--lister-micros`
- *Traitement > 2 s* → `"taille_modele": "base"` dans `config.json`, puis `--telecharger`
- *Mauvaise réponse proposée* → monter `seuil_correspondance` (ex. 70) ou ajouter des formulations
- *Ta voix n'est pas détectée* → baisser `seuil_energie` (ex. 0.005)
