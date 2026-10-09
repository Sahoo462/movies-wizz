# Movies Wizz backend

This dependency-free Python backend serves the movie app and provides a SQLite-backed API for the catalog, accounts, and personal watchlists. Curated 2026 candidates are in `featured_movies_2026.json`; Indian titles appear first, and supplied release dates are retained. A movie is included only when its catalog entry has an HTTPS poster URL. Candidates without a poster URL are not shown; no generated placeholders or online lookup guesses are used.

## Run locally

From the project folder, run:

```powershell
python backend\app.py
```

Then open <http://127.0.0.1:8000>. The database is created at `backend\data\movieswizz.sqlite3` on first startup. Set `MOVIES_WIZZ_DB` to use another database path.

## API

- `GET /api/health`
- `GET /api/movies?q=&year=&genre=&industry=&limit=&offset=`
- `GET /api/movies/{title}`
- `POST /api/auth/register` with `name`, `email`, and `password`
- `POST /api/auth/login` with `email` and `password`
- `GET /api/auth/me`
- `POST /api/auth/logout`
- `GET /api/watchlist`
- `POST /api/watchlist` with a `titles` array
- `DELETE /api/watchlist/{title}`

Authenticated routes use the `Authorization: Bearer <token>` header. Passwords are stored as PBKDF2-HMAC-SHA256 hashes, and only hashes of session tokens are stored.

## Tests

```powershell
python -m unittest discover -s backend\tests -v
```
