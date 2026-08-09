# preparations plastiques
##placer les inserts à chaud
## presser les aimant dans la coque arriere

# assemblage moteurs 

## identification des moteurs et preapration des cables (conseillé de faire des etiquettes d'apres le schema')
## poser tous les servo dans leurs emplacment
## viser les moteurs des gqchette qvec les vis plastiques (fourneis avec les servo)

# electronique : 
## connection des pin sur gpio (seleon schema)
## modifier le cable usb pour permettre une alimentation externe 

# assemblage moluvemnt 

## alimenter en 5v et placer l'esp 32 en ""calibration"" (appuyer sur boot) => led bleue allumée : les motuers sont en position de montage pret pour installé les ocmposnts

## fleches : 
- placer les vis plastique sur les extremités d'un guignol double (180 degres)
- viser deux vis plastique au 5eme trou de chaque coté
- securiser sur le moteur avec une vis M2.5 (fournies avec le servo)
- demonter la fleche grise originale et remplacer la partie interrieru avec celle imprimée

## bouton colores : 
- insaller dique : fleche imprimé a la surface vers le haut
- installer elemnt sous les boutons et viser les vis plastique fournies avec les servos en s'assurant qu'elles depassent toute de la meme longueur

## joysitck x2 :
- viser les parties attachés au moeurs 
- percer le tube de ptfe avec une vis plastique forunie avec le servo à l'aide de l'outil 
- couper le tube à env 28mm et couper en point sur le haut
- redresser le tube ptfe 
- installer le tube de ptfe
- placer la partie plasrtique sous le joystick et le glisé sur le tube 

## trigger R et L 
- recuperer les triger sur la manette d'origine
- couper les coté poour leur donner un aspect lisse
- installer usr les moteurs en securisant avec des vis M2.5 (fournies avec le servo)

## securiser les moteurs avec les colson et ne profiter pour inclure leur cable dans le colson

# cable management 
## viser plaque arriere et rassembler les cables comme fait sur la manette temoins
## colsonner le cable externe, l'esp 32 ains ique la bobine de cable gpio jaune

# ajustmeent dues frottements :
## placer la coque superieure

## ajuster la hauter des servo "boutons" et fleches à l'aide de la vis dans l'insert 
- sortir du mode "calibration" en pressant le boot sur l'esp32 pour les faire bouger en possition "haute" 
- les bouttons ou la fleche ne doivent pas etre au plus haut sous peine de forcer trop sur le motues

# ajuster la hauteur des joystick en tirant / poussant desuus 
- la flexibilité du ptfe permet egalement d'aguster le centrage du joystick'

# (optionnel) reajustmeent du centrage des joystick peut etre reliase en moficiant le fichier micropython "main_offline" 
- utiliser thonny
- connecter l'esp en pressant "boot" pour annuler le demarage automatique du code et acceder aux archives micropython

# YEEEHHPEEEE coffee time !!!
