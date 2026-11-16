# HR Bridge — HH.ru → Bitrix24

MVP for importing applicant responses from HeadHunter into Bitrix24 leads.

## Features
- HH.ru employer OAuth connection
- Import responses by vacancy ID
- Applicant contact details and source metadata
- Duplicate checks by phone and email
- Bitrix24 lead creation
- Demo import and event log

## Run locally
```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app:app --host 0.0.0.0 --port 8000
```

Configure `HH_CLIENT_ID`, `HH_CLIENT_SECRET`, `HH_REDIRECT_URI`, and `BITRIX_WEBHOOK_URL` before live use.

## Current scope
Avito integration is not included yet; it requires API access and credentials. Resume download availability depends on employer permissions and HH response data. For production, add authenticated admin access, encrypted token storage, robust pagination/retries, and configure portal-specific custom Bitrix24 fields.
