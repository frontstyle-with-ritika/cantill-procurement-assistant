# Corvo Procurement Assistant

A policy-driven procurement prototype for the Cantill.ai Full-stack Intern challenge. It provides employee chat intake, batch PR processing, a buyer queue, policy preview/approval, preferred-supplier exports, and audit history.

## Run with Docker

If Docker Desktop is installed, run this one command in the project folder:

```powershell
docker compose up --build
```

Then open `http://localhost:8000`. The `data/` folder is mounted locally so the database and uploaded task kit stay together. Stop with Ctrl+C.

## Run directly on Windows

Docker is optional. From PowerShell in this folder:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000`. Keep the terminal open while using the app. Interactive API documentation is at `http://127.0.0.1:8000/docs`.

## Load the challenge data

The challenge kit is deliberately excluded from Git with `.gitignore`. It is already under `data/Cantill_FullStack_Task_Kit/` for local development. For a hosted instance, open **Policy & data → Import task kit** and upload the supplied ZIP. The app reads the item, supplier, PO, contract, and PR CSVs from that runtime data folder.

The baseline v5.0 rules are stored as application configuration in `app/policies/policy_v5.json`. The source PDFs, sample PR batch, catalogue, supplier data, and contracts stay out of the public repository. Import the task-kit ZIP at runtime to load the reference data.

## Main pieces

- `app/main.py`: chat intake and the FastAPI app entry point.
- `app/api.py`: batch, buyer, policy, data-import, and contract endpoints; SQLite audit storage.
- `app/decision_engine.py`: item matching, clubbing, valuation, supplier ranking, route selection, and PO consolidation.
- `app/policy_engine.py`: loads date-effective JSON policy versions, validates amendments, compares changes, and evaluates rule conditions.
- `app/static/`: browser interface and its styling/JavaScript.
- `DECISIONS.md`: assumptions where the supplied policy or data is silent.
- `APPROACH.md`: tools and implementation summary.
- `LEARNING_GUIDE.md`: plain-language architecture and interview preparation.

## Batch workflow

In **PR batch**, choose the supplied CSV and select **Process batch**. The app returns one decision row per input PR line, stores it in the SQLite buyer queue, and offers a CSV download. The same area exports the top three active preferred suppliers for each item on a chosen date. In **Buyer queue**, approving a PO records its PO reference; approving a sourcing decision creates an RFQ, RFP, or sourcing reference. Override needs a written reason. Actions record actor and timestamp. These are app records, not transactions sent to a real purchasing system.

## Policy workflow

In **Policy & data**, load or upload a JSON policy amendment, preview the changed clauses, and select **Approve and activate**. PDF text can be extracted into the reference box; translate the amendment into the structured JSON policy, review the preview, then activate it. The decision engine selects the version effective on each requirement date. Changing rule data does not require changing Python code.

## Deployment (free demo)

Connect the public repository to Render's free web-service plan. Use build command `pip install -r requirements.txt` and start command `uvicorn app.main:app --host 0.0.0.0 --port $PORT`. After deployment, upload the task-kit ZIP at runtime.

Free hosting can sleep, restart, or have temporary local storage. Render's free web services spin down after inactivity and do not provide persistent disks. Re-import task data and policy after a restart if needed. Avoid selecting paid plans or adding paid services.

## Repository hygiene

Do not commit the supplied raw task-kit files, `data/`, `.venv/`, SQLite database, PDFs, or CSVs. The `.gitignore` excludes these files. `app/policies/policy_v5.json` is the app's structured baseline rules, not a copy of the source batch or master data; it is included so the policy screen works before the reference-data ZIP is imported.

## Blind-test files

When the organizers send the blind batch, export run 1 under v5.0, activate the amendment through **Policy & data**, then export run 2 and the preferred-supplier list. Rename the three CSV downloads to `yourname__run1.csv`, `yourname__run2.csv`, and `yourname__preferred.csv`, put them in `submission/`, and commit that folder before the stated cutoff. The `submission/` folder is intentionally not ignored.
