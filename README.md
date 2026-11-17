# NHTSA Lakehouse RAG

Assistant automobile qui répond à des questions **textuelles** et **chiffrées** sur les rappels et plaintes véhicules, construit sur un **lakehouse incrémental de bout en bout**.

> Exemples de questions :
> - « Quels rappels concernent le freinage des Golf 2019 ? »
> - « Combien de plaintes moteur pour Renault en 2025 ? »

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

- [ ] **Phase 1 — Socle** : docker-compose (MinIO, Postgres + pgvector, Dagster), ingestion NHTSA → bronze (JSON brut horodaté), écriture Iceberg. *Livrable : `docker compose up` charge les données brutes.*
- [ ] **Phase 2 — Transformations & qualité** : silver (nettoyage, normalisation constructeur/modèle/année, dédoublonnage), gold (faits rappels, faits plaintes, dimensions véhicule/composant), Soda (nulls, unicité, fraîcheur), tests dbt en CI, assets Dagster + schedule quotidien incrémental. *Livrable : pipeline autonome avec alerte qualité.*
- [ ] **Phase 3 — Indexation RAG** : documents depuis le gold (1 rappel = 1 document) et les PDFs (PyMuPDF), 2 stratégies de chunking, embeddings dans pgvector, BM25 via tsvector, ré-indexation incrémentale par hash de contenu. *Livrable : index toujours synchronisé avec le lakehouse.*
- [ ] **Phase 4 — Assistant** : retrieval hybride (RRF) + reranking cross-encoder, citations obligatoires, « je ne sais pas » sous un seuil (ROC), agent à 2 outils (`search_docs`, `query_sql` en lecture seule sur tables autorisées), FastAPI + Streamlit.
- [ ] **Phase 5 — Évaluation & observabilité** : jeu de 40–60 questions, recall@k, fidélité/hallucinations (RAGAS ou LLM-juge), exactitude SQL, latence, coût/requête ; comparaison vecteurs seuls / hybride / hybride + reranking ; Langfuse ; éval en CI qui fait échouer la PR si les scores baissent.
- [ ] **Phase 6 — Finition** : Terraform (VM + docker-compose) ou démo vidéo, README final (choix, limites, métriques).

## Choix de conception

- **Pas de Spark ni de Kubernetes** : DuckDB suffit largement pour ce volume.
- **pgvector plutôt que Qdrant** : un seul Postgres pour vecteurs, plein texte (BM25) et métadonnées → recherche hybride sans service supplémentaire.
- **Iceberg** : tables versionnées, évolution de schéma, time-travel, sur stockage objet.
- **Périmètre progressif** : 1–2 constructeurs au départ, puis élargissement.

Le différenciant : **l'incrémental de bout en bout** (nouvelle donnée → transformée → contrôlée → indexée) et **l'évaluation chiffrée**.

## Structure

```
ingestion/      scripts d'ingestion NHTSA et PDFs → bronze
dbt/            modèles silver / gold
quality/        contrôles Soda
orchestration/  assets et schedules Dagster
rag/            chunking, embeddings, indexation, retrieval
agent/          agent, outils, API FastAPI
ui/             application Streamlit
eval/           jeu de test et scripts d'évaluation
infra/          docker-compose, Terraform
data/pdfs/      PDFs techniques (non versionnés)
```

## Métriques

*À venir (phase 5).*
