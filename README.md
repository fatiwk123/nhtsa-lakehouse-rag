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
docker compose run --rm ollama-pull
```

Le premier run complet (bronze → gold → Soda → Iceberg → PDFs → index) prend une dizaine de minutes ;
`ollama-pull` télécharge une seule fois le LLM local (`OLLAMA_MODEL`, ~2,5 Go).

| Service | URL |
|---|---|
| Assistant (Streamlit) | http://localhost:8501 |
| API (FastAPI, doc OpenAPI) | http://localhost:8000/docs |
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
- [x] **Phase 4 — Assistant** : retrieval hybride (RRF) + reranking cross-encoder, citations obligatoires, « je ne sais pas » sous un seuil (ROC), agent à 2 outils (`search_docs`, `query_sql` en lecture seule sur tables autorisées), FastAPI + Streamlit.
- [x] **Phase 5 — Évaluation & observabilité** : jeu de 40–60 questions, recall@k, fidélité/hallucinations (RAGAS ou LLM-juge), exactitude SQL, latence, coût/requête ; comparaison vecteurs seuls / hybride / hybride + reranking ; Langfuse ; éval en CI qui fait échouer la PR si les scores baissent.
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
| Documents | 61 rappels + 175 PDFs (37 Mo) |
| Chunks | 1 824 (944 `structure`, 880 `fixed`) |
| 1ʳᵉ indexation | 5 min 37 (CPU), run Dagster complet depuis zéro |
| Passage sans changement | **0 document ré-embeddé, 2,8 s** |
| 1 document modifié + 1 supprimé | 1 ré-embeddé (5 chunks), 1 supprimé, 8 s |

Premier constat, qui motive la phase 4 : pour une question en français (« freinage des Golf 2018 »), la recherche vectorielle seule remonte des rappels Golf hors sujet, alors que la recherche plein texte trouve le bon rappel (18V369000). D'où la fusion hybride et le reranking.

Essai en ligne de commande : `docker compose run --rm pipeline python -m rag search "brake caliper"`

## Assistant (phase 4)

```
question ─► recherche hybride ─► passages [S1..S5] ─┐
            (vecteur + plein texte, RRF,            ├─► LLM local (Ollama) ─► outils ─► réponse citée
             rerank cross-encoder, seuil)           │      search_docs / query_sql      ou « je ne sais pas »
                                                    └─ tables gold (DuckDB, lecture seule)
```

- **Retrieval en 3 modes** (comparés en phase 5) : `vector`, `hybrid` (Reciprocal Rank Fusion des classements pgvector et plein texte, k = 60) et `hybrid_rerank` (les 30 candidats fusionnés sont reclassés par `cross-encoder/mmarco-mMiniLMv2-L12-H384-v1`, multilingue : questions en français, documents en anglais).
- **« Je ne sais pas » sous un seuil** : si le meilleur passage a un score de reranking < `RAG_MIN_RERANK_SCORE` (0 par défaut), la recherche est déclarée non pertinente. Sur les essais, les vraies questions donnent des scores de +3 à +6, et les questions hors périmètre (« Renault Clio », « tarte aux pommes ») de −4 à −6. Le seuil sera calibré en phase 5 sur le jeu d'évaluation.
- **Agent à 2 outils** : `search_docs` (recherche hybride, passages numérotés à citer) et `query_sql` (comptes, listes et classements sur le gold). Les règles sont **appliquées dans le code**, pas seulement demandées au modèle :
  - les passages sont recherchés pour la question avant le premier appel au LLM (les petits modèles oublient souvent l'outil de recherche) ;
  - une question quantitative (« combien », « le plus »…) doit passer par `query_sql` : quelques passages ne sont pas une statistique ;
  - sans preuve (aucun passage pertinent, aucune ligne SQL), la réponse est la phrase fixe « Je ne sais pas… », quoi qu'ait écrit le modèle ;
  - les citations `[S#]` qui ne correspondent à aucun passage renvoyé sont supprimées ;
  - après un échec d'outil (SQL invalide, résultat vide), le modèle a droit à une relance avec l'erreur ;
  - les nombres de la réponse absents des résultats d'outils sont signalés (`unsupported_numbers`), un premier indicateur d'hallucination pour la phase 5.
- **SQL en lecture seule, défense en profondeur** :
  - une seule requête `SELECT`, vérifiée avec le parseur de DuckDB ;
  - uniquement les 5 tables gold autorisées ;
  - connexion `read_only` sans accès aux fichiers (`enable_external_access = false`) ;
  - au plus 50 lignes, interruption après 10 s.

  Les tests vérifient le refus de `DELETE`, `DROP`, `ATTACH`, `COPY`, `read_csv`, de plusieurs requêtes et des autres schémas.
- **LLM local** : `qwen3:4b-instruct` via Ollama, sans clé ni donnée qui sort de la machine. Les autres modèles testés :
  - `qwen2.5:3b` écrit du SQL faux et invente des chiffres ;
  - `qwen2.5:7b` donne de bonnes réponses, mais met 2 à 4 min par question sur CPU ;
  - `qwen3:4b`, la version « thinking », raisonne pendant plusieurs minutes avant chaque appel d'outil.

  Le modèle se change avec `OLLAMA_MODEL`.
- **API** FastAPI (`/search`, `/ask`, `/health`) et **interface** Streamlit : la réponse affiche ses sources (titre, page, lien NHTSA), la requête SQL exécutée et le déroulé des outils.

Exemples (Volkswagen 2018-2020, CPU 12 cœurs, sans GPU) :

| Question | Outils | Réponse | Latence |
|---|---|---|---|
| Combien de plaintes moteur pour Volkswagen déposées en 2025 ? | SQL | 52 (exact) | ~3 min |
| Quels modèles VW ont le plus de plaintes avec incendie ? | SQL | TIGUAN 4, JETTA 3, TIGUAN LIMITED 3… (exact) | ~50 s |
| Pourquoi les Golf 2018 ont-elles été rappelées pour le freinage ? | recherche | pistons d'étriers mal revêtus, rappel 18V369000, cité | ~1 min 30 |
| Quel est le remède du rappel 18V369000 ? | recherche | purge des étriers, cité (rappel + rapport PDF p. 1) | ~2 min |
| Quels sont les rappels Renault Clio ? | recherche | « Je ne sais pas » (hors périmètre NHTSA) | ~15 s |

La latence vient de la génération du LLM sur CPU, à environ 7 tokens/s. Un GPU, ou une API hébergée, la ramènerait à quelques secondes.

## Évaluation et observabilité (phase 5)

Le jeu d'évaluation [`eval/dataset.yaml`](eval/dataset.yaml) compte 56 questions, écrites à partir du corpus Volkswagen 2018-2020 :
- **34 questions documentaires** (défaut, cause, remède), chacune avec ses rappels pertinents et, pour certaines, les termes que la réponse doit contenir ;
- **14 questions chiffrées** (comptes, classements, années), chacune avec une requête SQL de référence exécutée au moment de l'éval, pour que la valeur attendue suive les données ;
- **8 questions hors périmètre** (Renault, recette de cuisine, prix…), auxquelles l'assistant doit répondre « je ne sais pas ».

### Retrieval : vectoriel vs hybride vs hybride + rerank

`python -m eval.retrieval_eval` (sans LLM, en CI). Un passage est pertinent s'il appartient à un rappel attendu.

| Mode | Chunking | Recall@1 | Recall@3 | Recall@5 | Recall@10 | MRR | Latence p50 |
|---|---|---|---|---|---|---|---|
| vectoriel | structure | 0,69 | 0,85 | 0,96 | 1,00 | 0,83 | 114 ms |
| vectoriel | fixed | 0,66 | 0,84 | 0,91 | 0,97 | 0,82 | 70 ms |
| hybride (RRF) | structure | 0,38 | 0,65 | 0,76 | 0,97 | 0,59 | 115 ms |
| hybride (RRF) | fixed | 0,57 | 0,84 | 0,94 | 1,00 | 0,76 | 89 ms |
| hybride + rerank | structure | 0,74 | 0,84 | 0,96 | 0,99 | 0,85 | 4,9 s |
| **hybride + rerank** | **fixed** | **0,81** | **0,97** | **1,00** | **1,00** | **0,93** | 6,3 s |

Ce que montre l'éval :
- **Le reranking apporte le plus** : MRR de 0,83 à 0,93, et le bon rappel est en tête 81 % du temps au lieu de 69 %.
- **La fusion RRF seule dégrade le vectoriel.** Les questions sont en français, la branche plein texte en anglais : elle n'accroche que des mots communs (« Golf », « 2018 ») et pollue la fusion. Le cross-encoder multilingue corrige ce bruit. Piste d'amélioration : traduire la requête avant la branche plein texte.
- **Le chunking `fixed` l'emporte une fois reranké**, d'où `RAG_STRATEGY=fixed` par défaut. Le chunking `structure` donne des chunks plus courts sur les fiches de rappel.
- **Le seuil du « je ne sais pas » est calibré sur l'éval** : AUC = 1,00 entre questions documentaires et hors périmètre. La question pertinente la plus faible obtient +1,74, la question hors sujet la plus forte −1,96. Le seuil à 0 est au milieu de cet écart, et le meilleur seuil (Youden) vaut −0,11.

Le choix de la stratégie et du seuil se fait sur ces 56 questions : c'est un petit jeu, et un jeu de test séparé serait plus rigoureux.

### Assistant de bout en bout

`python -m eval.agent_eval [--judge]`, en local, car environ 2 h sur CPU pour les 56 questions. LLM `qwen3:4b-instruct`. Résultats bruts : [`eval/results/agent.json`](eval/results/agent.json).

| Métrique | v1 | v2 |
|---|---|---|
| Exactitude globale | 84 % | **88 %** |
| Documents : réponse correcte et citée | 88 % | 91 % |
| Documents : précision des citations | 99 % | 98 % |
| SQL : valeur exacte | 64 % | 71 % |
| Hors périmètre : « je ne sais pas » | 100 % | 100 % |
| Réponses avec un nombre absent des résultats d'outils | 11 % | 11 % |
| Latence p50 / p95 | 130 s / 179 s | 135 s / 187 s |
| Tokens / question (appels LLM) | 3 236 (1,46) | 3 407 (1,52) |
| Coût / question | 0 $ (local) | 0 $ (local) |

L'écart entre v1 et v2 vient de l'analyse des 9 échecs de la v1 :
- **SQL** : le modèle comptait `count(*)` sur `fct_recalls`, qui a une ligne par campagne et par véhicule. Le schéma donné au LLM précise maintenant de compter `DISTINCT campaign_number` (13 rappels airbag au lieu de 21).
- **Garde-fou** : une question chiffrée sans requête SQL réussie reçoit désormais « je ne sais pas » plutôt qu'un nombre tiré de quelques passages.
- **Notation** : la réponse « vidage des étriers » au rappel 18V369000 était juste, mais la liste de termes attendus ne prévoyait que « purge » ou « bleed ». La liste a été élargie ; c'est une correction de la notation, pas du système.
- **Relance** : quand le modèle refuse alors que des passages pertinents lui ont été donnés, il a droit à une relance.

Les échecs restants viennent tous du modèle de 4 milliards de paramètres, pas du retrieval :
- **3 refus à tort** : le bon rappel est en tête des passages et l'information figure dans le texte, mais le modèle refuse quand même, y compris après la relance. Ce mode d'échec est sûr : il refuse au lieu d'inventer.
- **4 requêtes SQL fausses** : `fire` au lieu de `crash`, un groupement par modèle au lieu de par année, `count(*)` malgré la consigne.

Un modèle plus grand ou un GPU sont les leviers évidents.

La fidélité est mesurée par trois indicateurs :
- les citations vérifiées : 98 % pointent vers un rappel attendu ;
- les nombres absents de tout résultat d'outil (`unsupported_numbers`) ;
- un **LLM-juge** optionnel (`--judge`), qui vérifie chaque affirmation contre le contexte fourni au modèle (`Answer.context`). Il n'est pas activé par défaut, car il ajoute un appel LLM par réponse sur CPU.

Pour estimer le coût avec un modèle hébergé, il suffit de renseigner `EVAL_PRICE_INPUT_PER_M` et `EVAL_PRICE_OUTPUT_PER_M`.

### Éval en CI

Le job `pipeline-and-eval` procède en trois étapes :
1. il reconstruit tout depuis zéro sur le périmètre de l'éval : API NHTSA, lakehouse, PDFs et index ;
2. il lance l'éval de retrieval ;
3. il compare 5 métriques à [`eval/baseline.json`](eval/baseline.json) : recall@1, recall@5 et MRR de la config de production, et les deux taux du seuil.

Une baisse de plus de 0,03, soit une question sur 34, **fait échouer la PR**. Le rapport s'affiche dans le résumé du job GitHub Actions et les résultats sont publiés en artefact. Après une amélioration voulue, `python -m eval.check_regression --update` met la baseline à jour.

L'éval avec LLM n'est pas en CI : à 2 min par question sur un runner CPU, ce serait trop lent. Le retrieval, déterministe, est le garde-fou automatique ; la qualité des réponses se mesure en local.

### Langfuse

Langfuse est optionnel :

```bash
docker compose --profile observability up -d langfuse   # http://localhost:3001 (admin@langfuse.local / admin12345)
# puis dans .env : LANGFUSE_PUBLIC_KEY=pk-lf-local, LANGFUSE_SECRET_KEY=sk-lf-local, et redémarrer l'api
```

Chaque question devient une trace :
- un span par outil (`search_docs`, `query_sql`), avec son entrée et sa sortie ;
- une génération par appel LLM, avec le prompt, la réponse et les tokens ;
- en métadonnées : « je ne sais pas », citations, nombres non soutenus, latence.

Langfuse v2 réutilise le Postgres de la stack, dans un schéma dédié. Sans les clés, le traçage est désactivé et rien ne change.

## Limites connues

- L'API NHTSA est publique mais limite les rafales : le client espace ses requêtes (`NHTSA_MIN_INTERVAL_S`), utilise 2 workers et Dagster n'exécute qu'une étape NHTSA à la fois.
- L'assistant tourne sur CPU : de 15 s à 3 min par question, et Ollama a besoin d'environ 3,5 Go de RAM. Il traite une requête à la fois (`OLLAMA_NUM_PARALLEL=1`) pour tenir dans 8 Go de mémoire Docker.
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
rag/            documents, chunking, embeddings, index pgvector incrémental, retrieval hybride (RRF, rerank),
                agent, outils search_docs / query_sql, client Ollama, traçage Langfuse
api/            API FastAPI (/search, /ask, /health)
ui/             application Streamlit
eval/           jeu d'évaluation (56 questions), éval retrieval / assistant, garde-fou de régression
infra/          init Postgres, Terraform
data/pdfs/      PDFs techniques (non versionnés)
```

## Métriques

| | |
|---|---|
| Lakehouse (VW 2018-2020) | 141 rappels, 3 242 plaintes, 61 campagnes, 175 PDFs, 49 nœuds dbt, 20 contrôles Soda |
| Index | 236 documents, 1 824 chunks ; 2ᵉ synchronisation sans changement en 2,8 s |
| Retrieval (hybride + rerank, `fixed`) | recall@5 = 1,00, MRR = 0,93 |
| « Je ne sais pas » | 8/8 questions hors périmètre refusées, AUC du seuil = 1,00 |
| Assistant (56 questions, LLM local 4B) | 88 % d'exactitude, 98 % de citations correctes, 0 $ par question |
| Latence (CPU, sans GPU) | ~2 min 15 par question (p50) |

Détail et méthode : section *Évaluation et observabilité*.

## Auteure

Fatima Ezzahra Wankida — Data Engineer junior · [LinkedIn](https://www.linkedin.com/in/fatima-ezzahra-wankida)
