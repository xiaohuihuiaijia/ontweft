<p align="center"><img src="frontend/public/brand/ontweft-favicon.svg" width="64" height="64" alt="Ontweft mark" /></p>
<h1 align="center">Ontweft · 序枢</h1>
<p align="center"><strong>Connect knowledge to business.</strong><br />Turn documents and databases into editable business ontologies with traceable evidence.</p>
<p align="center">AI-assisted modeling · Interactive graphs · Self-hosted · Apache-2.0</p>
<p align="center"><a href="README.md">简体中文</a> · English · <a href="#get-started">Get started</a> · <a href="https://github.com/xiaohuihuiaijia/ontweft/blob/codex/slim-enterprise-beta/docs/deployment.md">Deployment</a> · <a href="https://github.com/xiaohuihuiaijia/ontweft/issues">Share a use case</a></p>

![The real Ontweft workbench: named business relationships and an editable customer-view object](docs/images/graph-workbench.png)

<p align="center"><sub>Real application and API, 22 synthetic objects and 30 relationships. Personal layout arranged with actual drag gestures. No customer data.</sub></p>

> **Try Enterprise Beta.** This page and the commands below refer to `codex/slim-enterprise-beta`, under review in [PR #1](https://github.com/xiaohuihuiaijia/ontweft/pull/1). Application code on `main` is still the earlier Alpha. The UI and detailed guides are primarily Chinese.

## Give your data a shared business meaning

A product manual, a spreadsheet and a database often describe different parts of the same business. Ontweft helps you define their types, properties and relationships, then map records into objects you can search, trace and edit.

**Choose sources → describe the business goal → review model and fact candidates → publish and import → keep relationships current.**

| Workflow | What you can do |
| --- | --- |
| **Understand documents** | Read PDFs, Office files, images and text; extract from a selected scope automatically or against your own requirements; open the supporting source |
| **Draft an ontology** | AI suggests types, properties, relations and mappings; reuse existing definitions, edit candidates, then validate, approve and publish |
| **Bring in data** | CSV/XLSX and PostgreSQL; explicit business keys, immutable file versions, mappings and exact cross-source identity binding |
| **Maintain relationships** | Recompute saved rules as matching fields change; preserve manual relationships, source contributions and historical evidence |
| **Work in the graph** | Authorized global search, paginated neighbors, drag and pin, saved personal views; click nodes to edit, inspect named relationships, and add or remove manual relationships |
| **Collaborate** | Project and field permissions, model revisions, versioned evidence and audit records; separate model author and approver in enterprise mode |

Five main destinations: **Data, Ontology, Objects, Graph, Settings**. One PostgreSQL database, one API service and the existing worker. No separate graph or vector service required. Manual modeling and deterministic ingestion work without an AI key; additional connectors, knowledge and automation remain optional.

## Get started

Install **Python 3.12, uv and Node.js 22.12+**, then run:

```sh
git clone --branch codex/slim-enterprise-beta https://github.com/xiaohuihuiaijia/ontweft.git
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

The local demo uses persistent SQLite in `var/local-demo/` and binds only to loopback. Team deployments use PostgreSQL and HTTPS. See the [deployment guide](https://github.com/xiaohuihuiaijia/ontweft/blob/codex/slim-enterprise-beta/docs/deployment.md).

## AI proposes. You decide what becomes a fact.

The integration uses configurable OpenAI-compatible Chat Completions. Text, vision and structured-output capabilities are declared and tested separately. Bailian is the first live-tested service; interface compatibility does not imply that other providers have been verified.

PDF pages render locally with native text extraction; configured vision models interpret table structure and complex pages. Office files and spreadsheets retain directly readable structure. Inputs include PDF, DOCX, XLSX, PPTX, PNG/JPEG, CSV, text and Markdown. Office page numbers denote logical units. Document uploads default to **256 MiB**; deterministic table imports have separate [size and structure limits](https://github.com/xiaohuihuiaijia/ontweft/blob/codex/slim-enterprise-beta/docs/data-to-ontology.md).

Page progress and evidence versions remain traceable. Changing the business goal can reuse base recognition results. Candidates are editable; models cannot run arbitrary SQL, publish definitions directly or bypass import confirmation. **No standalone OCR engine, GPU or resident local model service is required. Configured external models incur provider charges.**

## What has been verified

Checks include isolated database regressions, real Chromium workflows, Linux/PostgreSQL CI and synthetic deployment/restore exercises. This audit fixes source projection scope, relationship indexes, historical evidence navigation, UI races and repeated queries. See the [audit](https://github.com/xiaohuihuiaijia/ontweft/blob/codex/slim-enterprise-beta/docs/project-audit.md) and [current CI](https://github.com/xiaohuihuiaijia/ontweft/pull/1/checks).

A synthetic benchmark covers **100,000 objects / 1,000,000 relationships / 50 users**. Unique-code search P95 still exceeds the one-second target; this is not a production SLA. Human-verified document accuracy, full-document processing, live CDC failure recovery and large-attachment disaster recovery still need dedicated acceptance tests. [Measurements and limits →](https://github.com/xiaohuihuiaijia/ontweft/blob/codex/slim-enterprise-beta/docs/beta-verification.md)

## Documentation and contributions

| Goal | Guide |
| --- | --- |
| Build your first ontology | [Getting started](https://github.com/xiaohuihuiaijia/ontweft/blob/codex/slim-enterprise-beta/docs/getting-started.md) · [Data to ontology](https://github.com/xiaohuihuiaijia/ontweft/blob/codex/slim-enterprise-beta/docs/data-to-ontology.md) |
| Understand models and graphs | [Methodology](https://github.com/xiaohuihuiaijia/ontweft/blob/codex/slim-enterprise-beta/docs/ontology-methodology.md) · [Graph operations](https://github.com/xiaohuihuiaijia/ontweft/blob/codex/slim-enterprise-beta/docs/graph.md) |
| Deploy, upgrade and extend | [Deployment](https://github.com/xiaohuihuiaijia/ontweft/blob/codex/slim-enterprise-beta/docs/deployment.md) · [Core and extensions](https://github.com/xiaohuihuiaijia/ontweft/blob/codex/slim-enterprise-beta/docs/enterprise-beta.md) |
| Review or contribute | [Contributing](https://github.com/xiaohuihuiaijia/ontweft/blob/codex/slim-enterprise-beta/CONTRIBUTING.md) · [Audit and open decisions](https://github.com/xiaohuihuiaijia/ontweft/blob/codex/slim-enterprise-beta/docs/project-audit.md) |

**Python / FastAPI / SQLAlchemy / PostgreSQL · React / TypeScript / Sigma / Graphology.** A modular monolith shaped by concrete use cases.

If this direction is useful to you, **Star** the repository to follow progress. [Share a use case or report an issue](https://github.com/xiaohuihuiaijia/ontweft/issues): what data are you connecting, what question should it answer, and where did you get stuck? Use sanitized examples. Report security concerns privately under the [security policy](SECURITY.md).

Project-owned code is [Apache-2.0](LICENSE). Third-party components retain their licenses and [notices](frontend/public/THIRD_PARTY_NOTICES.txt); see [screenshot provenance](docs/images/README.md) and [NOTICE](NOTICE). No claim of production readiness, a complete security audit, trademark clearance or complete binary/model redistribution clearance is made.
