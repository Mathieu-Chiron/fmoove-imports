# Import → Payt (multi-clients)

Outil interne : chaque client envoie ses deux exports FMS par email à son **alias**
(`client@mondomaine`) ; tous les alias arrivent dans une **boîte catch-all** unique.
L'application relève cette boîte, **route chaque email vers le bon client selon
l'adresse destinataire**, fusionne les fichiers en un CSV au format d'import Payt,
le contrôle, et le transmet à l'**API d'import de l'administration Payt de ce
client**. Chaque client a son alias et sa propre administration Payt (voir
`TENANTS_JSON`).

Payt traite les fichiers importés une fois par jour, généralement vers 1 h du
matin. Une réponse 2xx de l'API ne signifie donc pas que l'import a réussi : elle
confirme seulement que le fichier est accepté pour traitement. Le résultat se
vérifie le lendemain dans l'onglet Import de l'administration Payt.

## Fonctionnement

1. Le client envoie **un seul email** avec ses deux exports FMS (« base clients »
   et « tous documents ») en pièces jointes, à son alias `client@mondomaine`.
2. Un **cron horaire** appelle `POST /poll-inbox` ; l'app relève la **boîte
   catch-all** en IMAP, **route** chaque email vers le bon client selon l'adresse
   destinataire, ne retient que les expéditeurs autorisés, et identifie les deux
   fichiers **par leur contenu** (onglets Factures/Avoirs vs colonnes Raison
   sociale/Adresse).
3. Elle fusionne les deux classeurs et applique les contrôles :
   - **bloquants** — fichier vide, champ obligatoire manquant, facture rattachée à
     un client absent de la base, code postal français invalide. Rien n'est envoyé.
   - **avertissements** — email manquant, raisons sociales en doublon, chute de
     volume de plus de 30 % par rapport au dernier envoi. L'envoi a lieu.
4. Le CSV est encodé en base64 et transmis à l'API d'import de l'**administration
   Payt du client** (`POST /import/files/csv`) : token statique en en-tête
   `Authorization`, `import_token` dans le corps.
5. L'app **répond par email** (depuis la boîte du client) avec le rapport, marque
   le message « lu » (jamais rejoué), et archive le CSV + les sources.

Les clients sont **isolés** : creds Payt propres à chacun, et une erreur sur une
boîte n'empêche pas le traitement des autres.

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
  -t rg.fr-par.scw.cloud/fairmoove-payt/app:1.4.0 --push .

# 2. Namespace Serverless Containers  ->  note l'ID renvoyé
scw container namespace create name=fairmoove-payt region=fr-par

# 3. Conteneur (les secrets sont stockés chiffrés côté Scaleway)
#    TENANTS_JSON = le registre des clients, compacté sur une seule ligne.
scw container container create \
  namespace-id=<namespace-id> \
  name=fairmoove-payt \
  image=rg.fr-par.scw.cloud/fairmoove-payt/app:1.4.0 \
  port=8080 min-scale=0 max-scale=1 memory-limit-bytes=1GB mvcpu-limit=1000 \
  environment-variables.MAILBOX_IMAP_HOST=<host> environment-variables.MAILBOX_SMTP_HOST=<host> \
  environment-variables.MAILBOX_USER=imports@mondomaine environment-variables.MAILBOX_FROM=imports@mondomaine \
  secret-environment-variables.POLL_TOKEN=<valeur> \
  secret-environment-variables.MAILBOX_PASSWORD=<valeur> \
  secret-environment-variables.TENANTS_JSON="$(cat tenants.json)" \
  region=fr-par

# 4. Déclencher le déploiement, puis récupérer l'URL publique
scw container container deploy <container-id> region=fr-par
scw container container get <container-id> region=fr-par -o json | jq -r .public_endpoint

# 5. Cron horaire : relève la boîte de chaque client et lance les imports
scw container cron create container-id=<container-id> region=fr-par \
  schedule="0 * * * *" args='{"token":"<POLL_TOKEN>"}'
```

`GET /health` renvoie la liste des clients configurés et les problèmes de config.
**Ajouter un client** = ajouter une entrée à `TENANTS_JSON` puis `container update`
+ `deploy`.

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

## Ajouter / mettre en production un client

- Créer un **alias** `client@mondomaine` pointant vers la boîte catch-all, et
  ajouter son entrée dans `TENANTS_JSON` (`inbox_address`, `administration_code`,
  tokens Payt, `allowed_senders`). Aucune nouvelle boîte à créer.
- Vérifier la convention de nommage attendue par Payt (`PAYT_FILENAME_PATTERN`,
  par défaut `AAAAMMJJ.csv`).
- Activer l'import automatique dans l'onglet Import de son administration Payt,
  et la protection contre les fichiers vides.
- Faire un premier envoi de test avant de compter sur le flux quotidien.
