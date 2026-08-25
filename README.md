# Import Fairmoove → Payt

Outil interne à usage unique : Fairmoove dépose ses deux exports FMS, l'application
les fusionne en un CSV au format d'import Payt, le contrôle, et le transmet
automatiquement à l'API d'import de Payt.

Payt traite les fichiers importés une fois par jour, généralement vers 1 h du
matin. Une réponse 2xx de l'API ne signifie donc pas que l'import a réussi : elle
confirme seulement que le fichier est accepté pour traitement. Le résultat se
vérifie le lendemain dans l'onglet Import de l'administration Payt.

## Fonctionnement

1. Le client s'authentifie (HTTP Basic) et dépose « base clients » et « tous documents ».
2. L'application fusionne les deux classeurs sur la raison sociale du client.
3. Elle applique les contrôles :
   - **bloquants** — fichier vide, champ obligatoire manquant, facture rattachée à
     un client absent de la base, code postal français invalide. Rien n'est envoyé.
   - **avertissements** — email manquant, raisons sociales en doublon, chute de
     volume de plus de 30 % par rapport au dernier envoi. L'envoi a lieu, et un
     récapitulatif part par mail.
4. Le CSV est encodé en base64 et transmis à l'API d'import de Payt
   (`POST /import/files/csv`). Le token statique part en en-tête `Authorization`,
   le `import_token` dans le corps.
5. Le CSV et les deux fichiers sources sont archivés dans Object Storage.

## Deux déclencheurs, un même traitement

- **Page web** (`/import`) — dépôt manuel des deux Excel, rapport à l'écran.
- **Email** — le client envoie **un seul email** avec les deux exports FMS en
  pièces jointes à une boîte dédiée (`imports@…`). Un **cron horaire** relève la
  boîte en IMAP (`POST /poll-inbox`), identifie les deux fichiers **par leur
  contenu**, applique le même traitement, puis **répond par email** avec le
  rapport. Seuls les expéditeurs de `INBOUND_ALLOWLIST` sont acceptés ; les
  messages traités sont marqués « lus » (jamais rejoués).

## Règles de transformation

| Champ Payt | Source |
|---|---|
| `administration_code` | variable `PAYT_ADMINISTRATION_CODE` |
| `debtor_code` | base clients → `Référence` |
| `debtor_company_name` | base clients → `Raison sociale` |
| `debtor_post_*` | base clients → `Adresse`, `Code postal`, `Ville`, `Pays` (converti en ISO) |
| `invoice_number` | documents → `Référence` |
| `invoice_date` | documents → `Date`, convertie en ISO |
| `invoice_due_date` | `Date` + `Echéance` (« 30 jours » → +30 j, « 60 jours fin de mois » → fin de mois) |
| `invoice_total_amount_inc_vat` | documents → `Montant TTC` |
| `invoice_open_amount_inc_vat` | documents → `Montant dû` |

Périmètre retenu : factures et avoirs dont le montant dû est différent de zéro,
brouillons exclus. Les avoirs sortent en montants négatifs.

## Développement

```bash
pip install -r requirements-dev.txt
cp .env.example .env      # à compléter, jamais committé
pytest -q
ruff check .
uvicorn app.main:app --reload --port 8080
```

Les tests génèrent leurs propres classeurs. Aucune donnée réelle de débiteur
n'est committée dans ce dépôt, et il ne faut pas en ajouter.

## Déploiement (Scaleway, fr-par)

L'application tourne en Serverless Container avec scale à zéro : elle ne coûte
que pendant les quelques secondes d'un dépôt.

```bash
# 1. Registry + image (build amd64, y compris depuis un Mac Apple Silicon)
scw registry namespace create name=fairmoove-payt region=fr-par
scw config get secret-key | docker login rg.fr-par.scw.cloud -u nologin --password-stdin
docker buildx build --platform linux/amd64 --provenance=false \
  -t rg.fr-par.scw.cloud/fairmoove-payt/app:1.1.0 --push .

# 2. Namespace Serverless Containers  ->  note l'ID renvoyé
scw container namespace create name=fairmoove-payt region=fr-par

# 3. Conteneur (les secrets sont stockés chiffrés côté Scaleway)
scw container container create \
  namespace-id=<namespace-id> \
  name=fairmoove-payt \
  image=rg.fr-par.scw.cloud/fairmoove-payt/app:1.2.0 \
  port=8080 min-scale=0 max-scale=1 memory-limit-bytes=1GB mvcpu-limit=1000 \
  environment-variables.APP_USER=fairmoove \
  environment-variables.PAYT_ADMINISTRATION_CODE=<code> \
  environment-variables.IMAP_HOST=<host> environment-variables.IMAP_USER=imports@<domaine> \
  environment-variables.SMTP_HOST=<host> environment-variables.ALERT_FROM=imports@<domaine> \
  environment-variables.INBOUND_ALLOWLIST=<expediteur@client> \
  secret-environment-variables.APP_PASSWORD=<valeur> \
  secret-environment-variables.PAYT_API_TOKEN=<valeur> \
  secret-environment-variables.PAYT_IMPORT_TOKEN=<valeur> \
  secret-environment-variables.IMAP_PASSWORD=<valeur> \
  secret-environment-variables.SMTP_PASSWORD=<valeur> \
  secret-environment-variables.POLL_TOKEN=<valeur> \
  region=fr-par

# 4. Déclencher le déploiement, puis récupérer l'URL publique
scw container container deploy <container-id> region=fr-par
scw container container get <container-id> region=fr-par -o json | jq -r .public_endpoint

# 5. Cron horaire : relève la boîte email et lance les imports
scw container cron create container-id=<container-id> region=fr-par \
  schedule="0 * * * *" args='{"token":"<POLL_TOKEN>"}'
```

`GET /health` renvoie l'état et la liste des variables manquantes.

### Rollback

Les conteneurs sont versionnés par tag d'image : pour revenir en arrière,
redéployer le tag précédent.

```bash
scw container container update <container-id> \
  image=rg.fr-par.scw.cloud/fairmoove-payt/app:<tag-précédent>
scw container container deploy <container-id> region=fr-par
```

Aucune base de données, aucune migration : le rollback est immédiat et sans
perte. Les archives déjà écrites dans Object Storage ne sont pas affectées.

## Avant la première mise en production

- Renseigner l'`administration_code` réel et vérifier la convention de nommage
  attendue par Payt (`PAYT_FILENAME_PATTERN`, par défaut `AAAAMMJJ.csv`).
- Activer l'import automatique dans l'onglet Import de l'administration Payt.
- Activer la protection contre les fichiers vides côté Payt.
- Faire un premier envoi sur l'environnement de test Payt, pas en production.
