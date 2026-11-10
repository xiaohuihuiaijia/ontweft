<p align="center">
  <img src="frontend/public/brand/ontweft-favicon.svg" width="64" height="64" alt="Ontweft logo" />
</p>

<h1 align="center">Ontweft · 序枢</h1>

<p align="center"><strong>Connect knowledge to business.</strong><br />Turn scattered data into business objects and relationships your team can understand, edit and maintain.</p>

<p align="center">Open-source enterprise ontology workbench · Self-hosted · Apache-2.0</p>

<p align="center">
  <a href="README.md">简体中文</a> · English ·
  <a href="#try-it-locally">Get started</a> ·
  <a href="https://github.com/xiaohuihuiaijia/ontweft/blob/codex/slim-enterprise-beta/docs/deployment.md">Deployment</a> ·
  <a href="https://github.com/xiaohuihuiaijia/ontweft/issues">Feedback</a>
</p>

![The real Ontweft graph workbench with draggable nodes, named business relationships and an editable object inspector](docs/images/graph-workbench.png)

<p align="center"><sub>Actual application UI with 22 synthetic objects and 30 named relationships. No customer data. The application UI and detailed guides are currently primarily in Chinese.</sub></p>

> **Try the enterprise Beta branch.** This page describes `codex/slim-enterprise-beta`, currently under review in [PR #1](https://github.com/xiaohuihuiaijia/ontweft/pull/1). Application code on `main` is still the earlier Alpha. The commands below explicitly check out Beta.

## From source data to a working business graph

Define what customers, orders, assets and projects mean in your business. Bring in existing records, maintain their identity as sources change, and explore authorized relationships in a graph you can edit. Start with one useful scenario instead of modeling the whole company at once.

**Connect → Model → Validate and publish → Keep relationships updated → Explore and edit.**

| What you need | Available in Beta |
| --- | --- |
| Use your existing data | CSV/XLSX uploads, PostgreSQL sources and editable mappings, with original files and record locations preserved |
| Model business meaning | Editable types, properties, enums, named relationships, composite unique keys and cardinality constraints |
| Keep the graph current | Immutable file versions, business-key updates, explicit cross-source identity binding and persistent relationship rules |
| Work directly in the graph | Authorized full-dataset search, paginated neighborhoods, mouse/touch dragging, pinned nodes and personal views; open node or relationship editors directly |
| Collaborate with traceability | Project and field permissions, versioned models, source evidence and audit; separate model authors and approvers by default in enterprise mode |
| Keep deployment lean | One API service, PostgreSQL and the existing worker; knowledge, additional connectors and automation are optional |

Five default navigation entries: **Data, Ontology, Objects, Graph, Settings**. Local modeling and graph exploration need no AI API key, vector database or external model service.

## Try it locally

Prerequisites: **Python 3.12, uv and Node.js 22.12+**. The local learning workspace uses persistent SQLite:

```sh
git clone --branch codex/slim-enterprise-beta https://github.com/xiaohuihuiaijia/ontweft.git
cd ontweft
uv sync --locked --no-dev
npm --prefix frontend ci
npm --prefix frontend run build
uv run --locked --no-dev python scripts/start_local_demo.py
```

Set a password of at least 12 characters when prompted. Open **http://127.0.0.1:8020** and sign in as `owner` with that password.

1. Open **本体** (Ontology), create a changeset and choose a project/task or asset/location template.
2. Edit the model, then save, validate, approve and publish it.
3. Create records and relationships under **对象** (Objects), then open **图谱** (Graph) to explore and edit them.

For file ingestion, persistent relationships, full-text indexing and large-model validation, run the worker in another terminal:

```sh
uv run --locked --no-dev python scripts/start_local_demo.py worker
```

Upload your CSV/XLSX under **数据** (Data). Local data stays in `var/local-demo/`. This entry listens on loopback only; use the deployment guide for PostgreSQL, HTTPS and enterprise operation. Source database connections require an explicit host allowlist.

## Measured progress, with clear limits

As of **2026-11-10**, isolated core installation, real-browser workflows, Linux/PostgreSQL deployment and backup/restore have been exercised. Current-commit results are available in the [PR checks](https://github.com/xiaohuihuiaijia/ontweft/pull/1/checks); historical test batches are not a substitute for a green current build.

The scale fixture contains **100,000 objects, 1,000,000 relationships and 50 users**. It used four API processes on Windows and Docker PostgreSQL 17.11 with 6 CPU / 11.68 GiB available. Initial data and indexes were prebuilt; this is not ingestion throughput or a production SLA.

| Measurement | Observed result |
| --- | --- |
| Queries from 50 concurrent clients | 1,000 mixed requests succeeded; common-term search / neighbors / detail P95: 0.765 / 0.772 / 0.657 s |
| Unique-code full-text search | P95 **2.135 s**, above the 1-second target |
| 50 concurrent writes reaching both search and rule-generated relationships | 50/50 completed; P95 **22.420 s**, including waits and retries; observers received 25 temporary 503 responses |

The canvas shows a bounded, authorized working view: up to 500 nodes and 1,000 edges. It does not render the entire database at once. Real WAL-expiry CDC recovery, large initial imports, large attachment restores and sustained production loads still require separate acceptance testing. Full OWL/SHACL, general reasoning, natural-language queries and vector search are not core features; Feishu and complex OCR need their own configuration and validation.

[Full methodology, measurements and remaining work →](https://github.com/xiaohuihuiaijia/ontweft/blob/codex/slim-enterprise-beta/docs/beta-verification.md)

## Documentation and architecture

These guides target the Beta branch and are currently in Chinese:

| Goal | Guide |
| --- | --- |
| Build your first ontology | [Getting started](https://github.com/xiaohuihuiaijia/ontweft/blob/codex/slim-enterprise-beta/docs/getting-started.md) |
| Bring in files or database tables | [Data to ontology](https://github.com/xiaohuihuiaijia/ontweft/blob/codex/slim-enterprise-beta/docs/data-to-ontology.md) |
| Understand the model and graph | [Modeling methodology](https://github.com/xiaohuihuiaijia/ontweft/blob/codex/slim-enterprise-beta/docs/ontology-methodology.md) · [Graph interactions](https://github.com/xiaohuihuiaijia/ontweft/blob/codex/slim-enterprise-beta/docs/graph.md) |
| Deploy, upgrade and restore | [Deployment](https://github.com/xiaohuihuiaijia/ontweft/blob/codex/slim-enterprise-beta/docs/deployment.md) · [Core and extensions](https://github.com/xiaohuihuiaijia/ontweft/blob/codex/slim-enterprise-beta/docs/enterprise-beta.md) |
| Contribute and inspect the implementation | [Contributing](https://github.com/xiaohuihuiaijia/ontweft/blob/codex/slim-enterprise-beta/CONTRIBUTING.md) · [Project audit](https://github.com/xiaohuihuiaijia/ontweft/blob/codex/slim-enterprise-beta/docs/project-audit.md) |

Built with **Python / FastAPI / SQLAlchemy / PostgreSQL** and **React / TypeScript / Sigma / Graphology**. Backend modules live in `backend/ontology/`, frontend features in `frontend/src/`, and CDC adapter source in `integrations/debezium/`. The core is a modular monolith, with no separate graph database or search service required.

## Help shape the workbench

If this is useful to your team, **star the repository** to follow its progress. Specific use cases are especially welcome: which data you need to connect, what question you want to answer, and where the workflow gets in the way.

- [Open an issue](https://github.com/xiaohuihuiaijia/ontweft/issues) with a version, reproduction steps and sanitized sample data.
- [Contribute](https://github.com/xiaohuihuiaijia/ontweft/blob/codex/slim-enterprise-beta/CONTRIBUTING.md) documentation, accessibility improvements, test cases or measured performance fixes. Small, focused changes are preferred.
- Report security issues privately as described in [SECURITY.md](SECURITY.md).

Original code is licensed under [Apache-2.0](LICENSE), with attribution in [NOTICE](NOTICE). Dependencies retain their own licenses and [notices](frontend/public/THIRD_PARTY_NOTICES.txt). [Screenshot provenance and reproduction](docs/images/README.md) are included. Open source does not imply production readiness, a completed security or trademark review, or clearance to redistribute every third-party binary or model package.
