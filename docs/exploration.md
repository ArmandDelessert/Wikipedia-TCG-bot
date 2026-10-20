# Exploration des 4 sites — 20 octobre 2026

Notes de terrain pour écrire ou réparer les modules de `wikitcg_bot/sites/`. Aucune valeur de
cookie, aucun identifiant de compte ici : seulement la structure.

**Sources** : pages enregistrées depuis un navigateur connecté (états « pack prêt » et « en
attente »), bundles JavaScript de ces pages, requêtes en lecture seule faites depuis un poste
résidentiel avec les cookies exportés, et un test sans secret depuis un runner GitHub.
Ce qui est *déduit* du code JavaScript sans avoir été observé en réseau est marqué *(déduit)*.

## Constat principal : l'accessibilité depuis GitHub Actions

Requêtes sans cookie depuis un runner `ubuntu-latest` (client HTTP simple et Chromium) :

| Site | Résultat depuis un runner GitHub | Depuis un poste résidentiel |
|---|---|---|
| wikitcg.net | **403 `cf-mitigated: challenge`**, y compris sur `/api/*` | normal (aucun défi) |
| wikigacha.com | **403 `cf-mitigated: challenge`**, y compris sur `/api/*` | normal |
| wiki-masters.com | normal (`POST /api/packs/open` → 401 JSON) ; **Turnstile sur le formulaire de connexion** | normal |
| wiki-tcg.com | normal, pas de Cloudflare (nginx) | normal |

Le cookie `cf_clearance` des exports est lié à l'adresse IP et au User-Agent du navigateur qui a
passé le défi : il ne sert à rien depuis un runner. Contourner un défi anti-bot est hors du
périmètre de ce projet ; wikitcg.net et wikigacha.com demandent donc une exécution depuis une
connexion « domestique » (poste, NAS, Raspberry Pi), pas depuis un runner hébergé par GitHub.

## Cookies d'authentification

| Site | Cookie utile | Contenu | Durée | Renouvellement observé |
|---|---|---|---|---|
| wikitcg.net | `wtcg_session` | JWT HS256 (`sub`, `email`, `name`, `avatar`, `iat`, `exp`) | 14 jours | **Non** : `iat` inchangé après plusieurs heures d'usage. Une réémission près de l'expiration reste possible (le dépôt de référence gère un `Set-Cookie`). |
| wikigacha.com | `__Secure-next-auth.session-token` | session NextAuth | 30 jours, **glissante** | **Oui** : chaque `GET /api/auth/session` renvoie un cookie neuf valable 30 jours. L'ancien reste valide jusqu'à son échéance. |
| wiki-masters.com | `sb-<réf. projet>-auth-token.0` et `.1` | `base64-` + JSON Supabase `{access_token, refresh_token, expires_at, user}` coupé en 2 morceaux (limite de 4 Ko par cookie) | jeton d'accès **1 h**, jeton de rafraîchissement à usage unique | **Oui, en permanence** : chaque rafraîchissement invalide le jeton précédent. Utiliser la session exportée depuis le navigateur *et* depuis le bot déconnecterait l'un des deux. |
| wiki-tcg.com | `wikitcg_session` | jeton opaque (207 caractères) | 30 jours | non observé |

Les autres cookies (`__cflb`, `cf_clearance`, `__Host-next-auth.csrf-token`,
`__Secure-next-auth.callback-url`, `wikitcg_v`, `wikitcg_connu`, `wikitcg_s`, `wikitcg_entree`)
sont des cookies de suivi, de répartition de charge ou de flux de connexion : inutiles à
exporter.

## WikiMasters (wiki-masters.com)

- Next.js sur Vercel ; authentification **Supabase** (e-mail + mot de passe uniquement).
  Formulaire de connexion protégé par **Cloudflare Turnstile** (champ `cf-turnstile-response`).
- Stock : 1 pack toutes les **10 min** (3 min en PRO), plafond **10**. Page `/pulls` : texte
  « N / 10 paquets disponibles », « Prochain dans m:ss », bouton « Ouvrir ».
- `POST /api/packs/open`, sans corps. Succès : `{cards, packs_remaining, packs_last_regen_at,
  owned_copies}`. Échec : `{error, packs_remaining?}` ; **`human_verification_required: true`**
  signifie que le site exige un Turnstile (`POST /api/human-check {token}`) : le bot doit
  s'arrêter et prévenir, pas contourner. Non connecté : 401 `{"error":"Non autorisé"}` (vérifié).
- `GET /api/packs/special` → `{packs, available, is_vip, next_available_at}` ; `POST
  /api/packs/special {packId}` ouvre un pack spécial avec délai de réutilisation *(déduit)*.
  `GET/POST /api/packs/pro-daily` : réservé PRO. `POST /api/packs/grace` : rôle non déterminé.
- Non connecté (vérifié avec un Chrome sans cookie) : `GET /pulls` → 307 vers `/login`, page avec un champ
  e-mail, un mot de passe et un Turnstile, sans en-tête de défi.
- À 0 paquet, la page affiche un bouton d'**achat** (« Rechargez 10 paquets à 1,99 $ CAD », `POST
  /api/checkout`) : ne jamais le cliquer. Un compte VIP voit un bouton « grâce » (`POST /api/packs/grace`).
- La section « Packs Spéciaux » dépend d'un indicateur de build (`NEXT_PUBLIC_FEATURE_SPECIAL_PACKS`) qui
  semble éteint dans les captures : fonctionnalité non vérifiée.
- Le module n'ouvre aucune session par formulaire : la session vient de `login wikimasters` (profil Chrome
  dédié). Le parcours connecté n'a **pas** été vérifié en réel.

## Wikipedia Gacha (wikigacha.com)

- Next.js + **NextAuth** (Google). Défi Cloudflare depuis les runners (voir plus haut). Une
  bannière de consentement (CMP publicitaire) s'affiche au premier chargement et doit être
  refusée avant tout clic.
- La progression est **synchronisée côté serveur, par langue** : `GET /api/user/progress?lang=FR`
  (le paramètre `lang` est en majuscules ; sans lui le site répond pour `JP`). Le bot doit donc
  jouer dans la **même langue** que le compte (`?lang=FR`).
- Ouverture *(déduit du JS)* : `POST /api/pack {action:"init"}` renvoie un `packState` signé
  par le serveur `{balance, lastRefillAt, nonce, sig, bonusState}` ; `POST /api/gacha
  {packState, guaranteedSrPlus, preferDeferredSync, researchOptIn, lang, campaignTimeZone}`
  renvoie les cartes. Le client ajoute des gardes (« pack guard ») : piloter l'interface avec
  Playwright est plus sûr que de rejouer l'API.
- Interface : « PAQUETS QUOTIDIENS : 7 / 10 », « Prochain pack dans : m:ss », « ▲ TOUCHEZ POUR
  OUVRIR ▲ ». Stock vide : « Plus de packs » + « Regarder une pub pour recharger » (non
  automatisé).
- Missions quotidiennes : +2 packs par mission (ouvrir 5 packs, obtenir une carte SR+, ouvrir
  Wikipédia une fois, partager, regarder une pub). Les deux dernières ne sont pas automatisables
  sans simuler une action qui n'a pas eu lieu.
- Régénération mesurée : **1 pack par minute, plafond 10** (règle du client : `balance + floor((now -
  lastRefillAt) / 60 s)`, au plus 10). Les missions « ouvrir 5 packs » et « obtenir une carte SR+ »
  créditent +2 packs automatiquement pendant l'ouverture : le stock affiché peut dépasser 10 (11/10 vu).
- Synchronisation **différée** : `POST /api/gacha` répond `syncDeferred: true` ; le client rejoue un reçu
  par `POST /api/user/progress/replay` 5 à 17 s plus tard. Fermer le navigateur trop tôt laisse l'ouverture
  dans la file locale `sync_queue` (rejouée au run suivant avec un profil persistant).
- Base locale : IndexedDB `wiki-gacha-db` (stores `cards_fr`, `user_data`, `locks`, `sync_queue`). Un profil
  neuf démarre vide et importe le cloud en ~8 s ; l'application ne pousse jamais une base vide vers un cloud
  non vide.
- Chargement d'une page connectée : le site lance seul `POST /api/pack {action:"init"}` et `POST
  /api/user/progress/promote-legacy`. Réponse de `/api/gacha` : `cards[{id,title,rarity_rank,…}]`, `packKind`
  (normal, gold, rainbow, black). Raretés vues : C, UC, R, SSR, UR (SR et LR non observées).

## wiki-tcg.com

- Next.js derrière nginx (pas de Cloudflare), authentification **Firebase** (Google) :
  `POST /api/auth/session {idToken, …}` pose le cookie `wikitcg_session`. Le bot ne peut donc
  pas se connecter seul : il faut importer le cookie.
- Stock : 1 booster toutes les **15 min**, plafond **10**. État lisible sans navigateur :
  `GET /api/v1/battement?parts=compte` → `parts.compte.corps.player.packsStored` (aussi `dust`
  et `speciaux`). Non connecté, `player` vaut `null` (vérifié depuis un runner).
- Ouverture : **Server Action Next.js** `openPack` (`POST /fr/boosters`, en-tête `Next-Action` de 42
  caractères hexadécimaux qui **change à chaque déploiement**, corps `["fr", null]`) : ne pas la rejouer à la
  main. Le booster est **consommé au clic** (vérifié : fermer le navigateur dès la scène de révélation fait
  quand même baisser le stock) ; retourner les cartes ne fait aucun appel réseau. Piloter l'interface :
  `button.sachet[aria-label="Ouvrir"]`, stock dans `.reserve__ligne b`, puis `.opener[data-phase=reveal]` avec
  5 `.flipper__slot[data-rarity]` (titre dans `.tcg__name`), « Tout révéler », `.opener[data-fini="1"]`,
  « Ouvrir un autre booster ». Raretés vues : C, PC, R (E, L, M, P, FA déduites du JS).
- Une fenêtre « Récompense de connexion » recouvre la page tant qu'elle n'est pas acquittée ; le bot ne la
  touche pas (réclamation non autorisée).
- Sans cookie valide, `/api/v1/battement` répond quand même 200 avec `"player": null` (pas de 401).
- Boosters spéciaux (`speciaux`) : un booster **Légendaire** attend celui qui gagne le tutoriel
  de combat (`/fr/bataille/tutoriel`) ; à faire une fois à la main.
- Cookie `wikitcg_session` : aucun renouvellement observé (empreinte identique avant/après plusieurs
  chargements et ouvertures).

## wikitcg.net

- Astro + Cloudflare. API JSON propre et stable :
  `GET /api/nav/status` → `{freePacks, paidPacks, totalAvailable, nextRegenAt (ms), maxFreePacks: 5,
  ink, level, xp, …}` ; `GET /api/series`, `GET /api/collection` ; `POST /api/packs/open
  {seriesId}` (et `"mystery"` pour le pack mystère).
- Stock : plafond de **5** packs gratuits, un nouveau toutes les **~2 h 40** (161 min restantes juste
  après une ouverture, mesuré le 21/10/2026). `GET /api/packs/status` = `nav/status` + `completionPacks`
  (récompenses de série complète, `POST /api/packs/open-completion` : signalées, jamais ouvertes).
  `POST /api/packs/regen` est une recharge payante en encre : jamais appelée.
- Depuis un poste résidentiel, aucun défi : les appels authentifiés avec **le seul** cookie
  `wtcg_session` et un User-Agent honnête répondent 200 ; `cf_clearance` est inutile. Non connecté :
  401 `{"error":"unauthorized"}`.
- `POST /api/packs/open {"seriesId":"mystery"}` hors délai : 403 `{"error":"mystery_not_eligible"}` (refus
  métier, pas un échec de session). Succès d'un pack normal : `cards[]` avec `rarity` et `article.title`.
  `GET /api/series` est publique (15 séries, `mystery` absente) ; `GET /api/collection` ne liste que les
  séries déjà ouvertes (`series_id`, `owned`, `total_pulls`).
- Le module actuel est cohérent avec ces endpoints ; il n'est inutilisable que depuis un runner.

## À vérifier (non observé)

1. Quand `wtcg_session` (14 j) et `wikitcg_session` (30 j) sont-ils réémis, s'ils le sont ?
2. WikiMasters, tout le parcours connecté : forme réelle de `POST /api/packs/open`, stock dans le vrai DOM,
   fréquence de `human_verification_required`, renouvellement du jeton Supabase dans un Chrome headless à
   profil persistant (lancer `status --sites wikimasters` deux fois à plus d'une heure d'écart).
3. Packs spéciaux : WikiMasters (`/api/packs/special`) et wiki-tcg.com (`player.speciaux`, forme supposée).
4. Wikipedia Gacha : raretés SR et LR, packs gold et black, défi Cloudflare jamais rencontré (la branche qui
   le traite n'est testée que contre un faux site).
5. Récompenses non réclamées : « récompense de connexion » (wiki-tcg.com, 3 boosters au jour 2 sur 7),
   packs de complétion (wikitcg.net), missions (Wikipedia Gacha).
