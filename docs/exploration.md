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
- Le module actuel (`sites/wikimasters.py`) se connecte par formulaire : il n'a ni détection de
  Turnstile, ni gestion de `human_verification_required`, ni packs spéciaux.

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
- Rythme de régénération réel : à mesurer (les captures montrent un compte à rebours de
  quelques secondes, ce qui ne correspond pas à un plafond quotidien de 10).

## wiki-tcg.com

- Next.js derrière nginx (pas de Cloudflare), authentification **Firebase** (Google) :
  `POST /api/auth/session {idToken, …}` pose le cookie `wikitcg_session`. Le bot ne peut donc
  pas se connecter seul : il faut importer le cookie.
- Stock : 1 booster toutes les **15 min**, plafond **10**. État lisible sans navigateur :
  `GET /api/v1/battement?parts=compte` → `parts.compte.corps.player.packsStored` (aussi `dust`
  et `speciaux`). Non connecté, `player` vaut `null` (vérifié depuis un runner).
- Ouverture : **Server Action Next.js** `openPack` (identifiant observé de 42 caractères
  hexadécimaux, **change à chaque déploiement**) : ne pas la rejouer à la main. Piloter
  l'interface : `button.sachet[aria-label="Ouvrir"]` (texte masqué « N disponible »), stock dans
  `.reserve__ligne b`, puis une scène de révélation des cartes.
- Boosters spéciaux (`speciaux`) : un booster **Légendaire** attend celui qui gagne le tutoriel
  de combat (`/fr/bataille/tutoriel`) ; à faire une fois à la main.
- Le module `sites/wiki_tcg_com.py` reste à écrire.

## wikitcg.net

- Astro + Cloudflare. API JSON propre et stable :
  `GET /api/nav/status` → `{freePacks, paidPacks, totalAvailable, nextRegenAt (ms), maxFreePacks: 5,
  ink, level, xp, …}` ; `GET /api/series`, `GET /api/collection` ; `POST /api/packs/open
  {seriesId}` (et `"mystery"` pour le pack mystère).
- Stock : plafond de **5** packs gratuits, un nouveau toutes les ~2 h environ (compte à rebours
  « Prochain booster gratuit dans 2h 6m » observé).
- Depuis un poste résidentiel, une requête sans aucun cookie ne déclenche pas de défi
  (`/api/nav/status` → 401 JSON) ; avec le cookie `wtcg_session`, `status` et `series` répondent
  200. Non testé : l'appel authentifié sans `cf_clearance`.
- Le module actuel est cohérent avec ces endpoints ; il n'est inutilisable que depuis un runner.

## À vérifier (non observé)

1. Quand `wtcg_session` et `wikitcg_session` sont-ils réémis ? (Surveiller `Set-Cookie` sur
   plusieurs jours.)
2. Un vrai passage de `openPack` sur wiki-tcg.com avec capture réseau, pour confirmer que
   l'ouverture est bien effective au clic et non à la fin de la révélation.
3. WikiMasters : fréquence de `human_verification_required`, et rôle exact de `/api/packs/grace`.
4. Wikipedia Gacha : rythme de régénération et comportement de `/api/gacha` hors interface.
