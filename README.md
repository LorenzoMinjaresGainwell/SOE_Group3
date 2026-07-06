# Rural Health Opportunity Dashboard

Zero-build local proof of concept for reviewing rural health RFP/RFA opportunities.

The app uses a Python standard-library backend, CSV files as the local data store, and plain HTML/CSS/JavaScript for the dashboard. There is no React, Vite, npm, hosted backend, or database required.

## Run locally

```bash
python app.py
```

Then open:

```txt
http://localhost:8000
```

## Project structure

```txt
SOE_Group3/
  app.py
  requirements.txt
  README.md
  data/
    opportunities.csv
    sources.csv
    scoring_rules.csv
    status_history.csv
  services/
    csv_store.py
    gov_api_client.py
    neural_model.py
    scoring.py
  static/
    index.html
    styles.css
    app.js
```

## API endpoints

```txt
GET  /api/opportunities
GET  /api/opportunities/<id>
GET  /api/sources
GET  /api/scoring-rules
POST /api/opportunities/<id>/status
POST /api/refresh
```

Status updates are written back to `data/opportunities.csv` and appended to `data/status_history.csv`.
