# Wikipedia-TCG-bot

Ouvre automatiquement, depuis **GitHub Actions**, les boosters qui se regénèrent sur des jeux de
cartes à collectionner basés sur Wikipédia. Plus besoin de rester sur les sites : un workflow
planifié se connecte à votre compte, ouvre les packs disponibles et publie un résumé (packs
ouverts, raretés, cartes tirées) dans l'onglet *Summary* du run.

Le bot **n'ouvre que des packs**. Il ne touche ni au marché, ni aux enchères, ni aux échanges, et
ne simule aucun visionnage de publicité.

## État de chaque site

| Site | Module | Connexion | Ce qui est vérifié |
|---|---|---|---|
| [WikiMasters](https://www.wiki-masters.com/) | `sites/wikimasters.py` | e-mail + mot de passe (ou session exportée) | Connexion et boucle d'ouverture testées contre un faux site local avec un vrai Chromium. L'endpoint `POST /api/packs/open` est celui des bots Sephiroth-ctrl et JeanMichelChien. **Jamais lancé contre le vrai site.** |
| [wikitcg.net](https://wikitcg.net/) | `sites/wikitcg_net.py` | cookie `wtcg_session` | Logique testée contre une fausse API ; endpoints repris de dochreidte/wikitcg-completer. **Jamais lancé contre le vrai site.** |
| [Wikipedia Gacha](https://wikigacha.com/) | `sites/wikigacha.py` | session exportée depuis votre navigateur | **Expérimental.** Seuls les libellés des boutons (`TAP TO OPEN`, `BACK TO PACKS`) sont connus, d'après le bookmarklet de Clifeee. S'arrête quand le site propose une recharge par publicité. |
| [WikiTCG](https://wiki-tcg.com/) | `sites/wiki_tcg_com.py` | — | **Pas implémenté** : aucune référence, site non inspecté. Lancez le workflow *Probe* (voir plus bas). |

Les sites n'ont pas pu être inspectés lors du développement (accès réseau bloqué). Faites donc un
premier run en **simulation** et vérifiez le résultat avant de laisser tourner le planning.

## Mise en route

1. **Utilisez ce dépôt en privé** (voir [Sécurité](#sécurité-et-coût)).
2. Ajoutez vos identifiants dans *Settings → Secrets and variables → Actions → Secrets* :

   | Secret | Pour | Contenu |
   |---|---|---|
   | `WIKIMASTERS_EMAIL`, `WIKIMASTERS_PASSWORD` | WikiMasters | vos identifiants |
   | `WIKIMASTERS_STORAGE_STATE` | WikiMasters (alternative) | session exportée, si vous vous connectez autrement que par e-mail |
   | `WIKITCG_NET_SESSION` | wikitcg.net | valeur du cookie `wtcg_session` : connectez-vous, ouvrez les outils de développement → *Application/Stockage → Cookies* |
   | `WIKIGACHA_STORAGE_STATE` | Wikipedia Gacha | session exportée (voir ci-dessous) |

   Un site sans secret est simplement ignoré.
3. *Actions → Open packs → Run workflow*, avec **dry_run** coché. Le résumé indique pour chaque
   site s'il s'est connecté et combien de packs étaient disponibles.
4. Relancez sans `dry_run`. Si tout est bon, le planning (`cron`) prend le relais.

### Exporter une session (Wikipedia Gacha, ou WikiMasters hors e-mail)

```bash
pip install -r requirements.txt && python -m playwright install chromium
python scripts/save_session.py wikigacha
```

Connectez-vous dans la fenêtre qui s'ouvre, appuyez sur Entrée dans le terminal, puis collez le
contenu de `wikigacha.state.b64` dans le secret. Ce fichier donne accès à votre compte.

### Variables optionnelles (*Variables*, pas *Secrets*)

| Variable | Défaut | Effet |
|---|---|---|
| `MAX_PACKS_PER_RUN` | 10 | plafond de packs ouverts par site et par run |
| `WIKITCG_NET_SERIES` | automatique | série à ouvrir ; sinon celle où il manque le plus de cartes |

## Fréquence : ce qu'il faut savoir

Les packs **s'accumulent** jusqu'à un plafond (le résumé affiche `stock avant/plafond`). Un run
périodique suffit donc, tant qu'il passe avant que le stock soit plein. Si la colonne *Stock avant*
affiche souvent le plafond, des packs sont perdus : rapprochez les runs.

- Le cron par défaut est **toutes les heures** (`17 * * * *`) ; modifiez-le dans
  `.github/workflows/open-packs.yml`. Le minimum de GitHub est 5 minutes et les runs peuvent être
  retardés de plusieurs minutes : ce n'est pas du temps réel.
- Un run avec Chromium dure environ 2 à 3 minutes. Sur un dépôt **privé**, le quota gratuit est de
  2 000 minutes par mois : une fois par heure tient dedans, deux fois par heure non. Les dépôts
  publics n'ont pas cette limite.
- GitHub **désactive les workflows planifiés** d'un dépôt public sans activité depuis 60 jours ;
  réactivez-le dans l'onglet *Actions*.
- Si des identifiants sont refusés, le run échoue avec une annotation d'erreur : GitHub vous envoie
  alors un e-mail.

## Sécurité et coût

- Les secrets ne sont jamais affichés dans les logs, mais le **résumé et les logs d'un dépôt
  public sont publics** (nombre de packs, cartes tirées).
- Le cookie de wikitcg.net est renouvelé par le serveur à chaque appel ; le bot le conserve d'un
  run à l'autre dans le cache GitHub Actions (un secret ne peut pas être modifié depuis un
  workflow). Sur un dépôt public, le cache est lisible par des workflows de pull requests : gardez
  le dépôt privé, ou exigez l'approbation des contributeurs extérieurs.
- Automatiser un site peut contrevenir à ses conditions d'utilisation et conduire à une
  restriction du compte. Le bot s'espace de 2 à 4 s entre deux ouvertures et plafonne le nombre de
  packs par run ; ne le poussez pas plus loin. Vous utilisez ce projet à vos risques.

## Compléter le support de wiki-tcg.com (et fiabiliser le reste)

*Actions → Probe → Run workflow* décrit les 4 sites depuis un runner GitHub : boutons, champs de
formulaire, liens et appels réseau (jamais de cookie ni d'en-tête). Le résultat est dans le
*Summary* du run ; sur un dépôt privé, des captures d'écran sont aussi disponibles en artefact.
Avec les secrets `*_STORAGE_STATE` définis (`WIKI_TCG_COM_STORAGE_STATE` pour wiki-tcg.com), la
sonde parcourt les pages connectées. C'est la base pour écrire `wiki_tcg_com.py` et corriger
les modules si un site a changé.

## Développement

```bash
pip install -r requirements-dev.txt && python -m playwright install chromium
python -m pytest -q                       # tests UI ignorés sans CI=1 ni CHROMIUM_PATH
python -m wikitcg_bot --sites wikitcg_net --dry-run
```

Pour ajouter un site : créer `wikitcg_bot/sites/<site>.py` avec `name`, `label`,
`configured(settings)` et `run(settings) -> SiteResult`, puis l'enregistrer dans
`wikitcg_bot/sites/__init__.py`.
