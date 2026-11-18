# Approach

## Stack

- Python 3.14-compatible code using FastAPI for the backend API.
- Plain HTML, CSS, and JavaScript for the browser interface, so there is no frontend build tool or paid UI service.
- SQLite from Python's standard library for processed batch rows, buyer actions, and audit history.
- CSV files are loaded at runtime as reference data. pypdf extracts contract schedules and policy text locally.

## AI and cost

Codex was used as a coding assistant. The running app uses deterministic parsing, catalog token matching, and a JSON policy engine; it does not call a paid language-model API and needs no API key. The policy PDF text extractor is local. A procurement user reviews and edits the structured JSON before activating a policy amendment.

## Design

The browser sends chat text or CSV content to FastAPI. The decision engine combines each request with the locally imported reference data, evaluates the effective policy in its listed order, and returns structured decisions. Policy versions are JSON data. Batch decisions and buyer actions are stored separately in SQLite so a buyer can see what happened and when.

The app must be run with the supplied task kit imported at runtime. The `data/` directory and local SQLite database are excluded from the public source repository.
