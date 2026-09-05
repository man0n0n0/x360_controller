# Format des séquences JSON

Une séquence décrit les mouvements de la manette dans le temps. Le même
fichier est utilisé par `record.py` (écriture), `show.py` et
`control_host.py` (lecture) 

## Structure

```json
{
  "version": 1,
  "channels": ["joyL", "joyR", "trigL", "trigR",
               "motorA", "motorB", "button", "arrow"],
  "duration": 29.2813,
  "frames": [
    [2.0284, {"arrow": "up"}],
    [2.4451, {"arrow": null}],
    [3.1761, {"arrow": "right", "trigL": 0.5}]
  ]
}
```

| Clé | Rôle |
|---|---|
| `version` | version du format, toujours `1` |
| `channels` | liste des canaux existants, à titre informatif |
| `duration` | horodatage de la dernière image, en secondes |
| `frames` | les événements, dans l'ordre chronologique |

## Les `frames`

Chaque entrée est une paire `[temps, changements]` :

- **temps** — secondes depuis le début, nombre décimal ;
- **changements** — uniquement les canaux qui *changent* à cet instant.

C'est le point essentiel : on enregistre des **différences**, pas l'état
complet. Un canal absent d'une entrée garde la valeur qu'il avait avant.
Pour connaître l'état à un instant donné, on part de la position neutre
et on applique dans l'ordre toutes les entrées jusqu'à ce moment.

Un fichier reste donc court et lisible : une séquence où seul le stick
gauche bouge ne contient que des lignes `joyL`.

## Les canaux

| Canal | Valeur | Signification |
|---|---|---|
| `joyL`, `joyR` | `[x, y]`, chacun de `-1.0` à `1.0` | sticks gauche et droit |
| `trigL`, `trigR` | `0.0` à `1.0` | gâchettes LT et RT (`0.0` = relâchée) |
| `motorA`, `motorB` | `0.0` à `1.0` | intensité des moteurs de vibration |
| `button` | `"A"`, `"B"`, `"X"`, `"Y"` ou `null` | bouton maintenu ; `null` = relâché |
| `arrow` | `"left"`, `"up"`, `"down"`, `"right"` ou `null` | croix directionnelle (4 directions) |

Pour `joyL` / `joyR` : `x` négatif vers la gauche, `y` négatif vers le
haut.

## Appuis et relâchements

`button` et `arrow` ne sont pas des impulsions : ils restent actifs
jusqu'à ce qu'on les remette à `null`. Un appui s'écrit donc toujours en
deux entrées.

```json
[2.03, {"arrow": "up"}],
[2.45, {"arrow": null}]
```

Sans la deuxième ligne, le servomoteur resterait appuyé sur la touche
jusqu'à la fin de la séquence.

## Règles à respecter

- Les entrées doivent être **triées par temps croissant**.
- Les valeurs sont bornées ; au-delà, l'ESP32 les ramène dans l'intervalle.
- Un fichier peut être modifié à la main : c'est du JSON ordinaire.
- Il est prudent de terminer une séquence en remettant tout au neutre,
  pour ne pas laisser un moteur ou une gâchette engagé.

```json
[29.28, {"joyL": [0, 0], "joyR": [0, 0], "trigL": 0.0, "trigR": 0.0,
         "motorA": 0.0, "motorB": 0.0, "button": null, "arrow": null}]
```

## Envoyer une séquence par USB

L'ESP32 attend **un objet JSON par ligne**, terminé par `\n`. Les clés
sont exactement celles des `frames` : il suffit donc de réécrire chaque
entrée telle quelle, au bon moment.

Le débit (`115200`) est une formalité : le port est un USB CDC natif, pas
une liaison série derrière une puce d'adaptation, et les données passent
à la vitesse de l'USB quelle que soit la valeur indiquée.

```python
#!/usr/bin/env python3
"""Rejoue une séquence sur la manette, via USB."""

import json, time, serial

PORT = "/dev/ttyACM0"
SEQUENCE = "show.json"

NEUTRE = {"joyL": [0, 0], "joyR": [0, 0], "trigL": 0.0, "trigR": 0.0,
          "motorA": 0.0, "motorB": 0.0, "button": None, "arrow": None}

frames = json.load(open(SEQUENCE))["frames"]
ser = serial.Serial(PORT, 115200, timeout=0.1)

def envoyer(objet):
    ser.write((json.dumps(objet) + "\n").encode())

try:
    depart = time.monotonic()
    for t, changements in frames:
        # On attend l'horodatage absolu de l'entrée, et non la durée
        # écoulée depuis la précédente : additionner les écarts ferait
        # dériver la séquence sur la longueur.
        reste = t - (time.monotonic() - depart)
        while reste > 0:
            time.sleep(min(reste, 0.5))
            envoyer({"hb": 1})     # voir la sécurité ci-dessous
            reste = t - (time.monotonic() - depart)
        envoyer(changements)
finally:
    envoyer({"stop": 1})           # remet tout au neutre
    ser.close()
```

### Sécurité : le battement de cœur

Si l'ESP32 ne reçoit rien pendant **1,5 seconde**, il remet toutes les
sorties à zéro. C'est volontaire : un ordinateur qui plante ne doit pas
laisser une gâchette enfoncée sur la manette.

Une séquence peut très bien rester silencieuse plusieurs secondes entre
deux mouvements ; d'où le `{"hb": 1}` envoyé pendant les attentes, qui
n'a aucun effet sinon celui de signaler que le maître est toujours là.

### Autres commandes utiles

| Envoyé | Effet |
|---|---|
| `{"stop": 1}` | remet immédiatement toutes les sorties à zéro |
| `{"ping": 1}` | l'ESP32 répond `{"ev": "pong"}` |
| `{"get": 1}` | l'ESP32 renvoie son état complet |
| `{"hb": 1}` | battement de cœur, sans autre effet |

L'ESP32 émet aussi de lui-même `{"ev": "calib", "on": true}` quand on
appuie sur le bouton BOOT, et `{"ev": "failsafe"}` s'il a coupé les
sorties faute de nouvelles. Il n'est pas obligatoire de lire ces
messages, mais il faut vider le port de temps en temps, sinon les
écritures de la carte finissent par se bloquer.
