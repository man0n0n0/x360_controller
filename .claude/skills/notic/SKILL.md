---
name: notic
description: Génère une notice LibreOffice Writer (.odt) mise en page à partir d'un fichier Markdown, avec les photos de img/ intégrées (pas de liens externes). À utiliser quand on demande de formater/mettre en page une notice, de regénérer notice.odt, ou de convertir un .md en document Writer avec images.
---

# notic — notice Markdown → ODT

Produit un `.odt` Writer propre : titres numérotés, listes à puces, photos redimensionnées
et groupées en ligne quand plusieurs illustrent la même étape.

## Règles de contenu

- **Corriger les fautes de frappe** du Markdown source (il est écrit vite, en français, plein de coquilles).
  Lister les corrections dans la réponse finale.
- **Clarifier sans inventer** : découper les blocs en puces, ajouter des sous-titres de regroupement,
  sortir une étape noyée dans un titre. Ne jamais ajouter d'instruction technique absente du source.
- **Rester minimal** : pas de préambule, pas d'avertissements, pas de « conseils » ajoutés.
- Le titre du document est `Notice de montage`.

## Placement des images

Lire chaque photo de `img/` avec l'outil Read pour identifier le sujet, puis la rattacher
à l'étape correspondante. Écarter les photos floues ou au sujet non identifiable.

Correspondances établies (jeu de photos actuel) :

| Image | Étape |
|---|---|
| IMG_0591 | Préparations plastiques — coque avec disque imprimé / inserts |
| IMG_0588 | Électronique — ESP32 sur la plaque |
| IMG_0547, IMG_0543, IMG_0544 | Flèches — guignol coupé, pièce grise interne, découpe |
| IMG_0590 | Boutons colorés — 4 boutons + vis plastique |
| IMG_0572, IMG_0574, IMG_0581 | Joysticks — tube PTFE percé, coupe ~28 mm, pièce sous joystick |
| IMG_0583 | Sécurisation — platine complète servos + colsons |
| IMG_0589, IMG_0586, IMG_0587 | Cable management |
| IMG_0593 | **écartée** (floue) |

## Mise en page

Les images sont placées dans un tableau sans bordure (une ligne, N colonnes) pour les mettre
côte à côte. Dimensions en attributs HTML `width`/`height` — **le CSS `img{width}` est ignoré
par l'import HTML de LibreOffice**, sans attributs l'image occupe toute la largeur de page.

Toutes les photos sont en portrait 3:4 après redimensionnement :

| Nb d'images dans le bloc | width | height |
|---|---|---|
| 1 | 227 | 302 |
| 2 | 189 | 252 |
| 3 | 151 | 201 |

Squelette d'un bloc figure :

```html
<table class="fig"><tr>
<td><img src="img/IMG_XXXX.jpg" width="151" height="201"></td>
<td><img src="img/IMG_YYYY.jpg" width="151" height="201"></td>
</tr></table>
```

CSS du document :

```css
body { font-family: "Liberation Sans", Arial, sans-serif; font-size: 11pt; }
h1 { font-size: 20pt; }
h2 { font-size: 13pt; }
h3 { font-size: 11.5pt; }
li { margin-bottom: 2pt; }
table.fig { border: none; margin-top: 6pt; margin-bottom: 10pt; }
table.fig td { border: none; padding: 0 8pt 0 0; vertical-align: top; }
```

Écrire les accents en entités HTML (`&eacute;` …) : l'import LibreOffice est plus fiable ainsi.

## Chaîne de production

Travailler dans le scratchpad, puis copier le résultat en `notice.odt` à la racine du projet.

1. Redimensionner les photos (max 900 px, EXIF corrigé) dans `<work>/img/`
2. Écrire `<work>/notice.html`
3. Convertir en ODT Writer — **il faut `--infilter`**, sinon LibreOffice produit un
   document Writer/Web (`writerweb8_writer`) au lieu d'un vrai document texte
4. Intégrer les images : la conversion laisse des liens relatifs `../../img/*.jpg`.
   Les remplacer par des entrées `Pictures/` dans le zip ODT + le manifest.
5. Vérifier en convertissant en PDF et en lisant les pages rendues en PNG

Le script `build.py` fait les étapes 1, 3, 4. Depuis la racine du projet :

```bash
python3 .claude/skills/notic/build.py <work_dir>
```

Il attend `<work_dir>/notice.html` déjà écrit et produit `<work_dir>/notice_final.odt`.

Vérification :

```bash
soffice --headless --convert-to pdf <work>/notice_final.odt --outdir <work>/check
pdftoppm -png -r 45 <work>/check/notice_final.pdf <work>/check/p
```

puis lire les `p-*.png` avec Read. Viser ~4 pages pour la notice actuelle ; si le document
gonfle à une image par page, c'est que les attributs `width`/`height` manquent.
