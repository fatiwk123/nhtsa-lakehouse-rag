# NHTSA Lakehouse RAG

Assistant automobile qui répond à des questions **textuelles** et **chiffrées** sur les rappels et plaintes véhicules, construit sur un **lakehouse incrémental de bout en bout**.

> Exemples de questions :
> - « Quels rappels concernent le freinage des Golf 2018 ? »
> - « Combien de plaintes moteur pour Volkswagen en 2025 ? »

## Démarrage rapide

```bash
cp .env.example .env
docker compose up -d --build
docker compose logs -f pipeline   
```

| Service | URL |
|---|---|
| Dagster | http://localhost:3000 |
| Console MinIO | http://localhost:9001 (minioadmin / minioadmin) |
| Postgres | localhost:5432 (lakehouse / lakehouse) |

Le périmètre (constructeurs, années, modèles) se règle dans `.env`. Ensuite le pipeline tourne seul
chaque jour à 6 h (schedule Dagster `daily_pipeline`). Lancement manuel : bouton *Materialize all* dans
Dagster, ou `docker compose run --rm pipeline`.

Tests : `pip install -r requirements-dev.txt && pytest` (unitaires) ; la CI exécute aussi le pipeline
complet sur un petit périmètre (ingestion réelle, `dbt build` avec ses tests, Soda).

## Sources

- **API publique NHTSA** : rappels (recalls) et plaintes (complaints), mises à jour régulièrement → vrai incrémental.
- **PDFs techniques** : manuels, bulletins techniques (TSB).

Données structurées (pour le SQL) + texte (pour le RAG).

## Architecture

```
API NHTSA ─┐
           ├─► MinIO ─► Iceberg (bronze ─► silver ─► gold)
PDFs ──────┘                 │  dbt-duckdb + Soda
                             │  orchestré par Dagster
                             ▼
              ┌──────────── gold ────────────┐
              ▼                              ▼
   chunking + embeddings              tables SQL (lecture seule)
   pgvector + tsvector (BM25)                │
              │                              │
              └──► Agent (search_docs, query_sql) ◄──┘
                         │
                   FastAPI + Streamlit
                         │
                      Langfuse
```

| Brique | Choix |
|---|---|
| Stockage objet | MinIO |
| Format de tables | Apache Iceberg (pyiceberg, catalog SQL) |
| Transformations | dbt + DuckDB |
| Qualité | Soda + tests dbt |
| Orchestration | Dagster |
| Index vectoriel + plein texte | Postgres + pgvector + tsvector |
| Embeddings / reranking | sentence-transformers, cross-encoder |
| API / UI | FastAPI, Streamlit |
| Observabilité | Langfuse |
| Déploiement | docker-compose, Terraform (VM) |

## Feuille de route

- [x] **Phase 1 — Socle** : docker-compose (MinIO, Postgres + pgvector, Dagster), ingestion NHTSA → bronze (JSON brut horodaté), écriture Iceberg. *Livrable : `docker compose up` charge les données brutes.*
- [x] **Phase 2 — Transformations & qualité** : silver (nettoyage, normalisation constructeur/modèle/année, dédoublonnage), gold (faits rappels, faits plaintes, dimensions véhicule/composant), Soda (nulls, unicité, fraîcheur), tests dbt en CI, assets Dagster + schedule quotidien incrémental. *Livrable : pipeline autonome avec alerte qualité.*
- [x] **Phase 3 — Indexation RAG** : documents depuis le gold (1 rappel = 1 document) et les PDFs (PyMuPDF), 2 stratégies de chunking, embeddings dans pgvector, BM25 via tsvector, ré-indexation incrémentale par hash de contenu. *Livrable : index toujours synchronisé avec le lakehouse.*
- [ ] **Phase 4 — Assistant** : retrieval hybride (RRF) + reranking cross-encoder, citations obligatoires, « je ne sais pas » sous un seuil (ROC), agent à 2 outils (`search_docs`, `query_sql` en lecture seule sur tables autorisées), FastAPI + Streamlit.
- [ ] **Phase 5 — Évaluation & observabilité** : jeu de 40–60 questions, recall@k, fidélité/hallucinations (RAGAS ou LLM-juge), exactitude SQL, latence, coût/requête ; comparaison vecteurs seuls / hybride / hybride + reranking ; Langfuse ; éval en CI qui fait échouer la PR si les scores baissent.
- [ ] **Phase 6 — Finition** : Terraform (VM + docker-compose) ou démo vidéo, README final (choix, limites, métriques).

## Choix de conception

- **Pas de Spark ni de Kubernetes** : DuckDB suffit largement pour ce volume.
- **pgvector plutôt que Qdrant** : un seul Postgres pour vecteurs, plein texte (BM25) et métadonnées → recherche hybride sans service supplémentaire.
- **Iceberg** : tables versionnées, évolution de schéma, time-travel, sur stockage objet.
- **Périmètre progressif** : 1–2 constructeurs au départ, puis élargissement.

Le différenciant : **l'incrémental de bout en bout** (nouvelle donnée → transformée → contrôlée → indexée) et **l'évaluation chiffrée**.

## Couche bronze (phase 1)

1. Chaque appel API est déposé tel quel dans MinIO :
   `s3://lakehouse/raw/nhtsa/{recalls|complaints}/ingest_date=YYYY-MM-DD/make=.../{modele}_{annee}_{ingestion_id}.json`
2. Chaque enregistrement devient une ligne des tables Iceberg `bronze.nhtsa_recalls` et `bronze.nhtsa_complaints`
   (catalog SQL dans Postgres, fichiers dans `s3://lakehouse/warehouse`, partition par `ingest_date`) :
   `payload` (JSON brut) + lignage (`ingestion_id`, `ingested_at`, `request_url`, `raw_file`).
3. **Incrémental** : `row_hash` = SHA-256 du véhicule interrogé et du payload. Seules les lignes nouvelles ou modifiées sont ajoutées ; un 2ᵉ passage identique ajoute 0 ligne.

Premier chargement (Volkswagen 2018-2020) : 141 rappels et 3 242 plaintes.

## Silver, gold et qualité (phase 2)

```
bronze_nhtsa_recalls ─┐                    ┌─ fct_recalls ─────────────┐
                      ├─ dbt silver ─► gold ├─ fct_complaints           ├─► Soda ─► Iceberg silver.* / gold.*
bronze_nhtsa_complaints┘   (incrémental)    ├─ bridge_complaint_component│   (bloque si échec)
                                            └─ dim_vehicle, dim_component┘
```

- **dbt-duckdb** lit les tables Iceberg bronze via le plugin `iceberg` (pyiceberg) et construit les couches dans DuckDB.
- **Silver** (`silver_recalls`, `silver_complaints`, `silver_complaint_components`) :
  - normalisation constructeur/modèle (seeds `make_aliases`, `model_aliases`, ex. `TIGUAN LTD` → `TIGUAN LIMITED`) ;
  - parsing des dates (rappels `jj/mm/aaaa`, plaintes `mm/jj/aaaa`, sentinelle `12/31/1969` → NULL) ;
  - découpage des composants (« `SERVICE BRAKES, HYDRAULIC` » contient une virgule) ;
  - dédoublonnage sur la dernière ingestion ;
  - **incrémental par date** : seules les lignes bronze avec `ingested_at` > dernier traitement sont relues (`delete+insert`).
- **Gold** :
  - `fct_recalls` : grain campagne × véhicule, compter avec `count(distinct campaign_number)` ;
  - `fct_complaints` : grain plainte × véhicule, une plainte peut viser plusieurs véhicules, compter avec `count(distinct odi_number)` ;
  - `bridge_complaint_component` : relie plaintes et composants ;
  - `dim_vehicle`, `dim_component` : chaque composant NHTSA est rattaché à une catégorie métier (`BRAKES`/Freinage, `ENGINE`/Moteur…).
- **Qualité** : 37 tests dbt (unicité, non-nullité, intégrité référentielle, valeurs acceptées) puis 20 contrôles **Soda** sur le gold (nulls, unicité, fraîcheur < 2 jours, bornes, pas de date future).
- **Publication** : silver et gold sont réécrits dans Iceberg **seulement si la qualité passe**. Iceberg reste la source de vérité ; les consommateurs (RAG, agent SQL) n'ont pas besoin du fichier DuckDB.
- **Orchestration Dagster** : un asset par table (bronze, modèles dbt, Soda, publication), schedule quotidien, et un capteur `pipeline_failure_alert` qui envoie une alerte au webhook `ALERT_WEBHOOK_URL` (Slack, Teams, Discord) à chaque échec.

État actuel (Volkswagen 2018-2020) :
- 61 campagnes de rappel (141 lignes campagne × véhicule) ;
- 2 858 plaintes (3 242 lignes plainte × véhicule) ;
- 34 véhicules et 34 groupes de composants.

## Indexation RAG (phase 3)

```
gold.fct_recalls ──► 1 document par rappel ─┐
                                            ├─► 2 chunkings ─► embeddings E5 ─► pgvector (HNSW)
PDFs NHTSA (MinIO) ─► PyMuPDF, page à page ─┘   fixed / structure                 + tsvector (GIN)
```

- **PDFs** : pour chaque rappel du gold, l'endpoint NHTSA `safetyIssues` liste les documents déposés. On garde le *Part 573 Safety Recall Report* (dernière version seulement, les amendements étant quasi identiques) et les *Manufacturer notices to dealers* (bulletins techniques), 4 au plus par rappel. Les fichiers sont stockés dans MinIO et leurs métadonnées dans `bronze.nhtsa_recall_documents` (Iceberg). Un document déjà connu n'est jamais retéléchargé.
- **Documents** :
  - un rappel = un document : vue d'ensemble, résumé, conséquence, remède, avec en métadonnées la marque, les modèles, les années, la catégorie et la date ;
  - un PDF = un document : texte extrait page par page, chaque chunk conserve sa page pour les citations.
- **2 stratégies de chunking** (comparées en phase 5) :
  - `fixed` : fenêtre glissante de 180 mots, chevauchement de 40 ;
  - `structure` : regroupe paragraphes et phrases jusqu'à 220 mots, sans jamais traverser une section ni une page.
- **Embeddings** : `intfloat/multilingual-e5-small` (384 dimensions), modèle multilingue car les questions sont en français et les documents en anglais. Le titre du document est ajouté à chaque chunk avant l'embedding (en-tête contextuel). Le modèle est intégré à l'image Docker.
- **Recherche hybride sans outil en plus** : pgvector (HNSW, cosinus) et plein texte Postgres (`tsvector` pondéré titre/contenu, `ts_rank_cd`) dans la même table.
- **Indexation incrémentale** : chaque document a un hash de contenu, et une `index_version` dérive du modèle et des paramètres de chunking. Seuls les documents nouveaux ou modifiés sont ré-embeddés, ceux sortis du lakehouse sont supprimés, chaque document est commité atomiquement. Changer de modèle ou de paramètres ré-indexe tout automatiquement.
- **Dagster** : `bronze_recall_documents` → `rag_index`, avec le contrôle bloquant `rag_index_in_sync` : chaque rappel du gold est indexé, chaque document a ses deux stratégies, aucun n'a une version d'index périmée.

Volumes (VW 2018-2020) :

| | |
|---|---|
| Documents | 61 rappels + 190 PDFs (42 Mo) |
| Chunks | 2 326 (1 185 `structure`, 1 141 `fixed`) |
| 1ʳᵉ indexation | ~6 min (CPU) |
| Passage sans changement | **0 document ré-embeddé, 2,8 s** |
| 1 document modifié + 1 supprimé | 1 ré-embeddé (5 chunks), 1 supprimé, 8 s |

Premier constat, qui motive la phase 4 : pour une question en français (« freinage des Golf 2018 »), la recherche vectorielle seule remonte des rappels Golf hors sujet, alors que la recherche plein texte trouve le bon rappel (18V369000). D'où la fusion hybride et le reranking.

Essai en ligne de commande : `docker compose run --rm pipeline python -m rag search "brake caliper"`

## Limites connues

- L'API NHTSA est publique mais limite les rafales : le client espace ses requêtes (`NHTSA_MIN_INTERVAL_S`), utilise 2 workers et Dagster n'exécute qu'une étape NHTSA à la fois.
- Les PDFs scannés (sans texte) sont détectés et ignorés : il n'y a pas d'OCR.
- NHTSA ne couvre que les véhicules vendus aux États-Unis : pas de données Renault, par exemple. Les constructeurs européens présents sont notamment Volkswagen, Audi, BMW, Mercedes-Benz et Volvo.
- Pour respecter la contrainte de `soda-core-duckdb`, DuckDB est épinglé en 1.0.0.
- Le run lancé par `docker compose up` (service `pipeline`, en ligne de commande) n'est pas couvert par le capteur d'alerte ; les runs planifiés ou lancés depuis Dagster le sont.
- Les images MinIO officielles ne sont plus publiées publiquement ; la stack utilise l'image figée `bitnamilegacy/minio` (développement local uniquement).

## Structure

```
ingestion/      scripts d'ingestion NHTSA et PDFs → bronze
dagster_home/   configuration de l'instance et du workspace Dagster
tests/          tests unitaires (pytest, en CI)
dbt/            modèles silver / gold, seeds, tests
quality/        contrôles Soda
orchestration/  assets et schedules Dagster
rag/            documents, chunking, embeddings, index pgvector incrémental, recherche
agent/          agent, outils, API FastAPI
ui/             application Streamlit
eval/           jeu de test et scripts d'évaluation
infra/          init Postgres, Terraform
data/pdfs/      PDFs techniques (non versionnés)
```

## Métriques

*À venir (phase 5).*
