# Wikipedia-TCG-bot

Ouvre automatiquement, **depuis votre poste Windows**, les boosters gratuits qui se rechargent sur
quatre jeux de cartes à collectionner tirés de Wikipédia. Une tâche planifiée repasse toutes les
10 minutes, se connecte avec votre session, ouvre les packs disponibles et note les cartes tirées
dans un historique local.

Le bot **n'ouvre que des packs**. Il ne dépense rien, ne touche ni au marché, ni aux enchères, ni
aux échanges, ne réclame ni mission ni récompense, ne regarde aucune publicité et ne résout ni ne
contourne aucun défi anti-bot : si un site en présente un, le bot s'arrête et le dit.

## Pourquoi en local

Cloudflare bloque les runners de GitHub Actions pour `wikitcg.net` et `wikigacha.com` (réponse 403
`cf-mitigated: challenge`, mesurée le 20 octobre 2026), et le formulaire de connexion de WikiMasters
est protégé par un Turnstile. Depuis votre connexion habituelle, aucun de ces défis n'apparaît.
Autre avantage : aucun secret chez GitHub, le dépôt peut rester public.

## État de chaque site

| Site | Session | Ouverture | Vérifié en réel (21/10/2026) |
|---|---|---|---|
| [wikitcg.net](https://wikitcg.net/) | cookie `wtcg_session` (14 jours) | API HTTP, sans navigateur | oui : 1 pack gratuit, 5 cartes lues |
| [wiki-tcg.com](https://wiki-tcg.com/) | cookie `wikitcg_session` (30 jours) | état par API, ouverture par Chrome | oui : 5 boosters, état avant/après recoupé |
| [Wikipedia Gacha](https://wikigacha.com/) | cookie NextAuth (30 jours glissants) | Chrome, profil persistant, FR | oui : 6 packs, cloud 1279 → 1309 cartes, aucune baisse |
| [WikiMasters](https://www.wiki-masters.com/) | profil Chrome dédié (`login`) | `POST /api/packs/open` depuis la page | oui : 4 packs, vérifié après `login wikimasters` |

Rythmes de recharge : Wikipedia Gacha 1 pack par minute (plafond 10), WikiMasters 1 par 10 min
(plafond 10), wiki-tcg.com 1 par 15 min (plafond 10), wikitcg.net 1 pack gratuit toutes les ~2 h 40
(plafond 5). Le bot ne relance un site que lorsque son stock approche du plafond (voir
`wikitcg_bot/schedule.py`) ; `run --force` ignore ce planning.

## Mise en route

Prérequis : Windows, Python 3.12+, Google Chrome installé.

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
```

1. **Exportez vos cookies** depuis votre navigateur (extension « cookies.txt », format Netscape) pour
   `wikitcg.net`, `wiki-tcg.com` et `wikigacha.com`, et placez-les dans `Cookies/` sous le nom
   `<hôte>_cookies.txt` (par exemple `wikitcg.net_cookies.txt`). Ce dossier est ignoré par Git.
   **Ne le faites pas pour WikiMasters** (voir plus bas).
2. **WikiMasters** : `.\.venv\Scripts\python -m wikitcg_bot login wikimasters`, connectez-vous à la
   main dans la fenêtre Chrome qui s'ouvre (le bot ne voit jamais votre mot de passe), puis
   appuyez sur Entrée dans le terminal.
3. Vérifiez sans rien ouvrir : `.\.venv\Scripts\python -m wikitcg_bot status`
4. Un premier tour : `.\.venv\Scripts\python -m wikitcg_bot run`
5. Planifiez-le : `.\scripts\install_task.ps1` (toutes les 10 minutes ; `-Uninstall` pour retirer).

### Commandes

| Commande | Effet |
|---|---|
| `python -m wikitcg_bot` ou `run` | ouvre les packs des sites dont le stock le justifie |
| `run --sites wikigacha,wiki_tcg_com` | seulement ces sites, sans tenir compte du planning |
| `run --dry-run` / `status` | lit l'état et les sessions, n'ouvre rien |
| `login <site>` | connexion manuelle dans Chrome ; la session est conservée |

Codes de sortie : 0 tout va bien, 1 erreur, 2 session expirée ou refusée (reconnectez-vous ou refaites
l'export), 3 un autre run est en cours, 64 arguments invalides.

Réglages facultatifs : copiez `.env.example` en `.env` (nombre maximal de packs par run, navigateur,
fenêtre visible avec `HEADLESS=0`, série de wikitcg.net, pack mystère…).

## Suivre ce que fait le bot

Chaque passage écrit dans `.state/logs/bot.log` : début par site, stock lu, chaque pack ouvert avec ses
raretés, durée, et la raison précise d'un arrêt. Pour le suivre en direct :

```powershell
Get-Content .state\logs\bot.log -Wait -Tail 30
```

Lancé à la main, `run` affiche les mêmes lignes dans le terminal. Ctrl+C l'interrompt proprement
(Chrome refermé, cookies conservés, verrou libéré, code de sortie 130). La tâche planifiée utilise
`pythonw.exe` et n'ouvre aucune fenêtre ; après une mise à jour de ce dépôt, relancez
`.\scripts\install_task.ps1` pour qu'elle en profite.

## Où vont les données

Tout ce qui est sensible reste sur votre disque, dans des dossiers ignorés par Git :

| Chemin | Contenu |
|---|---|
| `Cookies/` | vos exports de cookies (lecture seule pour le bot) |
| `.state/sessions/<site>.json` | cookies courants, y compris ceux que les sites ont renouvelés |
| `.state/profiles/<site>/` | profils Chrome (WikiMasters, Wikipedia Gacha) |
| `.state/history.jsonl` | une ligne par run et par site : cartes tirées et raretés |
| `.state/backups/` | sauvegarde quotidienne de votre collection Wikipedia Gacha (côté serveur) |
| `.state/logs/bot.log` | journal du bot (jamais de valeur de cookie) |

Ces fichiers donnent accès à vos comptes : ne les partagez pas et ne les copiez pas dans le dépôt.

### Les cookies et leur renouvellement

Un export au format Netscape est un fichier texte de 7 colonnes séparées par des tabulations :
`domaine`, `inclut les sous-domaines`, `chemin`, `HTTPS seulement`, `expiration` (secondes Unix,
0 = cookie de session), `nom`, `valeur`.

- Le bot repart de l'export, puis **réécrit les cookies renouvelés** dans `.state/sessions/` à chaque
  run : ce qu'un site renouvelle est donc conservé sans rien faire. Un export plus récent que cet état
  le remplace. Exception : `wikigacha` travaille dans un profil Chrome persistant, qui ne reçoit que les
  cookies qui lui manquent ; si sa session est révoquée, supprimez `.state/profiles/wikigacha` avant de
  refaire l'export. Le cookie `cf_clearance` de Cloudflare n'est jamais conservé (lié à l'IP et au
  navigateur d'origine).
- Le journal `.state/logs/bot.log` reçoit une ligne par site et par run (y compris les arrêts propres).
  La tâche planifiée tourne sans fenêtre : c'est là qu'il faut regarder en cas de doute.
- `wikitcg.net` : 14 jours, aucun renouvellement observé. `status` prévient quand il reste moins de
  3 jours : refaites alors un export.
- **WikiMasters** utilise Supabase, dont le jeton de rafraîchissement ne sert qu'une fois. Réutiliser un
  export déconnecterait votre navigateur : le bot n'importe donc jamais cet export et travaille avec sa
  propre session, créée par `login wikimasters`.

## Limites connues

- Récompenses (autorisées par le propriétaire) : le bot récupère la « récompense de connexion » de wiki-tcg.com
  et ouvre les packs de complétion de wikitcg.net. Sur Wikipedia Gacha, les missions « 5 packs » et « SR+ » sont
  créditées par le site ; le bot valide aussi, une fois par jour, « Ouvrir Wikipedia une fois » (clic sur la pile
  de cartes, onglet refermé) et « Partager le résultat » (un clic sur le bouton suffit ; les alertes du site sont
  refusées, rien n'est publié). Les réclamations de wiki-tcg.com et de wikitcg.net n'ont été vérifiées que
  contre de faux sites (rien à réclamer le jour du test) ; la forme des packs de complétion est supposée.
- Le bot ne regarde **aucune publicité** : la mission « Regarder une publicité » et la recharge par pub de
  Wikipedia Gacha supposeraient de simuler un visionnage, ce qui fausserait les statistiques des annonceurs.
- Le pack mystère de wikitcg.net est demandé « à l'aveugle » (cadence inconnue, ~6 h supposées) ; les refus
  sont sans effet. `WIKITCG_NET_MYSTERY=0` désactive la demande.
- Les packs spéciaux de WikiMasters et de wiki-tcg.com ne sont pas vérifiés en réel.
- Se déconnecter d'un site dans le navigateur d'où vient l'export peut invalider la session exportée
  (non vérifié) : évitez-le, ou refaites un export ensuite.
- Automatiser un site peut contrevenir à ses conditions d'utilisation et conduire à la restriction
  du compte. Le bot espace les ouvertures de 2 à 4 secondes et plafonne chaque run (10 packs par
  site par défaut). Vous l'utilisez à vos risques.

## Développement

```powershell
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
.\.venv\Scripts\python -m pytest -q          # les tests de navigateur utilisent le Chrome installé
```

Les notes d'exploration des sites (endpoints, sélecteurs, pièges) sont dans
[`docs/exploration.md`](docs/exploration.md). Pour ajouter un site : créer `wikitcg_bot/sites/<site>.py`
avec `name`, `label`, `hosts`, `login_url`, `configured`, `session_summary` et `run`, l'enregistrer
dans `wikitcg_bot/sites/__init__.py` et ajouter sa cadence de recharge dans `wikitcg_bot/schedule.py`.
