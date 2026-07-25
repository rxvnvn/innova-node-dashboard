# API v1

The frontend depends only on `GET /api/v1/status`. The Python backend can later be replaced by an HTTP endpoint built into `innovad` while preserving the same frontend.

Fields may be `null` when unavailable. Existing v1 fields should not be renamed; new optional fields may be added.

Top-level sections: `api`, `dashboard`, `generated_at`, `node`, `chain`, `network`, `system`, `features`, `errors`.
