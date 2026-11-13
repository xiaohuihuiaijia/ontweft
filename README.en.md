<p align="center"><img src="frontend/public/brand/ontweft-favicon.svg" width="64" height="64" alt="Ontweft mark" /></p>
<h1 align="center">Ontweft · 序枢</h1>
<p align="center"><strong>Connect knowledge to business.</strong><br />Turn documents and databases into editable business ontologies with traceable evidence.</p>
<p align="center"><strong>Enterprise ontology · Knowledge graphs · Knowledge management</strong><br />AI-assisted modeling · Self-hosted · Apache-2.0</p>
<p align="center"><a href="README.md">简体中文</a> · English · <a href="#get-started">Get started</a> · <a href="docs/deployment.md">Deployment</a> · <a href="https://github.com/xiaohuihuiaijia/ontweft/issues">Share a use case</a></p>

![Ontweft product knowledge graph: products, inventory and use cases with relationship conditions and source evidence](docs/images/graph-workbench.png)

<p align="center"><sub>Real application and API with synthetic products, inventory and conditions. Drag to arrange the view; open objects or relationships to inspect properties and evidence. No customer data.</sub></p>

> **Current version: Enterprise Beta.** The default `main` branch includes the Beta application. Clone it and follow the steps below. The UI and detailed guides are primarily Chinese; document accuracy and production deployment still need acceptance testing for your own use case.

## From source material to traceable knowledge graphs

A product manual, a spreadsheet and a database often describe different parts of the same business. Ontweft helps you define their types, properties and relationships, then map records into objects you can search, trace and edit.

**Choose sources → describe the business goal → review model and fact candidates → publish and import → keep relationships current.**

| Workflow | What you can do |
| --- | --- |
| **Understand documents** | Read PDFs, Office files, images and text; extract from a selected scope automatically or against your own requirements; open the supporting source |
| **Draft an ontology** | Start from business questions; review reuse decisions, source evidence and model differences before validating, approving and publishing |
| **Bring in data** | CSV/XLSX and PostgreSQL; explicit business keys, immutable file versions, mappings and exact cross-source identity binding |
| **Maintain relationships** | Express applicability as relationship properties, retain conflicting candidates for review and record property history; incremental rules preserve manual and source support |
| **Work in the graph** | Authorized global search, paginated neighbors, drag and pin, saved personal views; edit objects or relationships and trace their source evidence |
| **Collaborate** | Project and field permissions, model revisions, versioned evidence and audit records; separate model author and approver in enterprise mode |

## Where Ontweft fits

| Your use case | What you build |
| --- | --- |
| **Product specifications** scattered across manuals and spreadsheets | Review evidence-linked candidates, define a product ontology, then search attributes and explore relationships |
| **Business knowledge graphs** spanning customers, orders and products | Bind deterministic records with business keys and navigate named relationships |
| **Knowledge management** with inconsistent concepts or untraceable conclusions | Find and reuse concepts through type aliases while retaining stable identifiers, versions and sources |
| **Knowledge base management** for documents and team knowledge | Enable the optional knowledge extension for pages, full-text search and evidence links; a turnkey Q&A chatbot is not included |

An **ontology** defines the kinds of things, their properties and allowed relationships. A **knowledge graph** connects actual objects under those definitions. A **knowledge base** retains documents and knowledge pages. Ontweft connects these layers; uploading a file alone does not make its content governed knowledge.

Five main destinations: **Data, Ontology, Objects, Graph, Settings**. One PostgreSQL database, one API service and the existing worker. No separate graph or vector service required. Manual modeling and deterministic ingestion work without an AI key; additional connectors, knowledge and automation remain optional.

## Get started

Install **Python 3.12, uv and Node.js 22.12+**, then run:

```sh
git clone https://github.com/xiaohuihuiaijia/ontweft.git
cd ontweft
uv sync --locked --no-dev
npm --prefix frontend ci
npm --prefix frontend run build
uv run --locked --no-dev python scripts/start_local_demo.py
```

Choose a password of at least 12 characters at the hidden prompt. Open **http://127.0.0.1:8020** and sign in as `owner`.

- **Explore the graph:** create a change set from a template in Ontology, edit and publish it, add records in Objects, then drag and edit them in Graph.
- **Use your own tables:** start the worker below in another terminal, upload CSV/XLSX in Data, choose business keys and generate editable model suggestions.
- **Try AI drafting:** configure a compatible service under Settings → Model connections, then select sources, scope and an optional business goal from Data or Ontology.

```sh
uv run --locked --no-dev python scripts/start_local_demo.py worker
```

The local demo uses persistent SQLite in `var/local-demo/` and binds only to loopback. Team deployments use PostgreSQL and HTTPS. See the [deployment guide](docs/deployment.md).

## AI proposes. You decide what becomes a fact.

![Ontweft AI model review: business questions, modeling reasons, source evidence and editable candidates](docs/images/ai-model-review.png)

<p align="center"><sub>Synthetic material and locally controlled model responses in the real review interface. No customer data; this is not a model-quality benchmark.</sub></p>

Review **why a concept belongs in the model** before adopting it. Modeling notes connect business questions to reuse or creation decisions and selected source material. Suggestions without matching evidence are flagged for review. Object and relationship candidates use field-aware editors; import results distinguish completed and blocked records rather than hiding conflicts behind a success message.

AI proposals and manual change sets share the same model diff: display changes, compatible additions and changes requiring data review. Impact counts come from the validation scan and stay unknown until measured. Type aliases help discovery; they do not merge objects.

The integration uses configurable OpenAI-compatible Chat Completions. Text, vision and structured-output capabilities are declared and tested separately. Bailian is the first live-tested service; interface compatibility does not imply that other providers have been verified.

PDF pages render locally with native text extraction; configured vision models interpret table structure and complex pages. Office files and spreadsheets retain directly readable structure. Inputs include PDF, DOCX, XLSX, PPTX, PNG/JPEG, CSV, text and Markdown. Office page numbers denote logical units. Document uploads default to **256 MiB**; deterministic table imports have separate [size and structure limits](docs/data-to-ontology.md).

Page progress and evidence versions remain traceable. Changing the business goal can reuse base recognition results. Candidates are editable; models cannot run arbitrary SQL, publish definitions directly or bypass import confirmation. **No standalone OCR engine, GPU or resident local model service is required. Configured external models incur provider charges.**

## What has been verified

Checks include isolated database regressions, real Chromium workflows, Linux/PostgreSQL CI and synthetic deployment/restore exercises. Ongoing fixes address reproduced failures in sequential file updates, source withdrawal previews, relationship matching, stale form state, model usage accounting and list queries. See the [audit](docs/project-audit.md) and [current CI](https://github.com/xiaohuihuiaijia/ontweft/actions/workflows/ci.yml).

A synthetic benchmark covers **100,000 objects / 1,000,000 relationships / 50 users**. Unique-code search P95 still exceeds the one-second target; this is not a production SLA. Human-verified document accuracy, full-document processing, live CDC failure recovery and large-attachment disaster recovery still need dedicated acceptance tests. [Measurements and limits →](docs/beta-verification.md)

## Documentation and contributions

| Goal | Guide |
| --- | --- |
| Build your first ontology | [Getting started](docs/getting-started.md) · [Data to ontology](docs/data-to-ontology.md) |
| Understand models and graphs | [Methodology](docs/ontology-methodology.md) · [Graph operations](docs/graph.md) |
| Deploy, upgrade and extend | [Deployment](docs/deployment.md) · [Core and extensions](docs/enterprise-beta.md) |
| Review or contribute | [Contributing](CONTRIBUTING.md) · [Audit and open decisions](docs/project-audit.md) |

**Python / FastAPI / SQLAlchemy / PostgreSQL · React / TypeScript / Sigma / Graphology.** A modular monolith shaped by concrete use cases.

Looking for a self-hosted ontology, knowledge graph or evidence-linked knowledge management tool? **Star** the repository to follow progress. [Share a use case or report an issue](https://github.com/xiaohuihuiaijia/ontweft/issues): what data are you connecting, what question should it answer, and where did you get stuck? Use sanitized examples. Report security concerns privately under the [security policy](SECURITY.md).

Project-owned code is [Apache-2.0](LICENSE). Third-party components retain their licenses and [notices](frontend/public/THIRD_PARTY_NOTICES.txt); see [screenshot provenance](docs/images/README.md) and [NOTICE](NOTICE). No claim of production readiness, a complete security audit, trademark clearance or complete binary/model redistribution clearance is made.
