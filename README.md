# x360_controller

Manette Xbox 360 « jouée » par des servomoteurs : sticks, croix, boutons
colorés, gâchettes et moteurs de vibration sont actionnés mécaniquement
depuis un ESP32-C3, qui reçoit ses ordres d'un ordinateur hôte. Le but est
de rejouer une séquence de mouvements enregistrée, en synchronisation avec
une vidéo, pour une installation qui tourne sans opérateur.

## Contenu du dépôt

| Dossier / fichier | Contenu |
|---|---|
| `hardware/` | sources FreeCAD (`.FCStd`) et STL prêts à imprimer : coques, plaques, supports servo, mécanisme joystick PTFE, outil de perçage |
| `software/esp/` | firmware MicroPython de l'ESP32-C3 (`main_offline.py`, `main_wifi.py`, `boot.py`, `lib/servo.py`, `tools/servo_helper.py`) |
| `software/raspberrypi/` | maître Linux `control_host.py` (interface Tk + enregistreur) et séquence de test `button_test.json` |
| `software/raspberrypi/control_example/` | exemple d'installation : `record.py` (éditeur séquence/vidéo), `show.py` (lecture plein écran synchronisée), `caption.webm` |
| `img/` | photos de montage et schéma de câblage (`img/GPIO_servo`) |
| `FORMAT_JSON.md` | format des séquences |
| `notice.odt`, `notice.pdf` | notice d'assemblage illustrée (le `.pdf` est exporté depuis le `.odt`) |

## Architecture

```
hôte Linux  ──USB CDC (JSON par ligne)──▶  ESP32-C3  ──PWM/RMT──▶  servos + L298N
control_host.py / show.py                  main_offline.py
```

Deux firmwares au choix, couche de contrôle identique :

- **`main_offline.py`** (recommandé) — pilotage par USB série natif. Une
  ligne = un objet JSON. Pas de radio, latence limitée par le rafraîchissement
  servo à 50 Hz.
- **`main_wifi.py`** — point d'accès Wi-Fi + WebSocket et interface web
  embarquée. Pratique sans ordinateur, plus lent.

`boot.py` importe `main` : renommer ou adapter l'import selon le firmware
retenu. Appuyer sur **BOOT** au démarrage annule le lancement automatique et
donne accès au REPL (Thonny).

## Câblage (ESP32-C3 SuperMini)

| GPIO | Fonction |
|---|---|
| 0 / 1 | stick gauche X / Y |
| 2 / 3 | stick droit X / Y |
| 20 | croix directionnelle (4 directions) |
| 21 | boutons colorés (A/B/X/Y) |
| 4 / 10 | gâchettes LT / RT — pilotées par **RMT**, les 6 canaux LEDC étant déjà pris |
| 5 / 6 | L298N IN1 / IN2 — moteur de vibration A |
| 7 / 8 | L298N IN3 / IN4 — moteur B ; GPIO8 est aussi la LED intégrée |
| 9 | bouton BOOT — bascule le mode calibration |

Les cavaliers ENA/ENB du L298N restent en place (enable câblé HIGH) ; la
vitesse est modulée sur les broches IN.

## Mise en route

```bash
# hôte
pip install pyserial              # + pygame et ffmpeg pour control_example/
python3 software/raspberrypi/control_host.py /dev/ttyACM0
```

- **Mode calibration** — appui sur BOOT, LED bleue allumée : tous les servos
  se placent en position de montage. C'est dans cet état qu'on installe les
  pièces mécaniques (voir la notice).
- **Failsafe** — sans message pendant 1,5 s, l'ESP32 remet toutes les sorties
  à zéro. L'hôte envoie `{"hb": 1}` quelques fois par seconde pendant les
  silences.

## Séquences

Format documenté dans [`FORMAT_JSON.md`](FORMAT_JSON.md) : JSON versionné,
images en **deltas** horodatés, éditable à la main. `control_host.py`
enregistre et rejoue ; dans `control_example/`, `record.py` permet de composer
une séquence face à une vidéo et `show.py` diffuse vidéo + séquence en plein
écran.

## Assemblage

Voir `notice.odt` / `notice.pdf` : inserts à chaud et aimants, pose des
servos, câblage GPIO, montage des guignols, tube PTFE des joysticks, gâchettes récupérées sur
la manette d'origine, puis réglage des hauteurs pour éviter les frottements.

## État

Révision 1. À corriger : friction sur les boutons colorés ; la notice
d'assemblage reste à compléter.
