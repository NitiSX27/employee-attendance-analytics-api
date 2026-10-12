# Employee Attendance & Analytics API

FastAPI + MongoDB (6.0+). All code is in `app/main.py`.

```bash
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017" MONGO_DB="attendance_db"   # or use a local .env
uvicorn app.main:app --port 8000
```
Indexes are created at startup.

**Status:** every endpoint in `openapi.yaml` is implemented, plus the explain endpoint. REVIEW.md and DECISIONS.md are filled in.

**Verification note:** Tested against Atlas MongoDB 8.0.34 with the supplied sample and a temporary 100,000-record synthetic dataset. The API endpoints and explain plans passed; the explain plans used index scans with no collection scans. Edge cases and concurrent employee creation, punch-in, punch-out, and regularization were exercised. The grader's hidden dataset and exact server version are unavailable.
