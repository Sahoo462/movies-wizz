from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qs, unquote, urlsplit


BACKEND_DIR = Path(__file__).resolve().parent
APP_DIR = BACKEND_DIR.parent
CATALOG_PATH = BACKEND_DIR / "movies.json"
FEATURED_CATALOG_PATH = BACKEND_DIR / "featured_movies_2026.json"
DATABASE_PATH = Path(
    os.environ.get("MOVIES_WIZZ_DB", BACKEND_DIR / "data" / "movieswizz.sqlite3")
)
PASSWORD_ITERATIONS = 310_000
SESSION_LIFETIME_SECONDS = 30 * 24 * 60 * 60
MAX_REQUEST_BYTES = 1_000_000
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
INDUSTRY_BY_LANGUAGE = {
    "Bengali": "Bengali",
    "English": "Hollywood",
    "Hindi": "Bollywood",
    "Kannada": "Sandalwood",
    "Malayalam": "Mollywood",
    "Marathi": "Marathi",
    "Tamil": "Kollywood",
    "Telugu": "Tollywood",
}
DEFAULT_STYLE = "linear-gradient(150deg,#454154 0%,#333448 48%,#171923 100%)"
LOGGER = logging.getLogger("movies_wizz")


def load_movies() -> list[dict[str, Any]]:
    with CATALOG_PATH.open(encoding="utf-8") as catalog_file:
        catalog = json.load(catalog_file)

    if not isinstance(catalog, list):
        raise ValueError("The movie catalog must be a JSON array.")

    movies: list[dict[str, Any]] = []
    movie_keys: set[tuple[str, int, str]] = set()
    for item in catalog:
        if not isinstance(item, dict):
            raise ValueError("Every movie in the catalog must be an object.")
        title = item.get("title")
        year = item.get("year")
        language = item.get("language")
        if (
            not isinstance(title, str)
            or not title.strip()
            or not isinstance(year, int)
            or isinstance(year, bool)
            or not isinstance(language, str)
            or not language.strip()
        ):
            raise ValueError("Every movie needs a title, year, and language.")

        normalized_title = title.strip()
        key = (normalized_title.casefold(), year, language.strip().casefold())
        if key in movie_keys:
            continue
        movie = {
            "title": normalized_title,
            "year": year,
            "genre": str(item.get("genre") or "Unclassified"),
            "language": language.strip(),
            "industry": str(
                item.get("industry")
                or INDUSTRY_BY_LANGUAGE.get(language.strip(), "International")
            ),
            "tag": str(item.get("tag") or "Catalog"),
            "style": str(item.get("style") or DEFAULT_STYLE),
        }
        poster_url = item.get("posterUrl")
        if year >= time.localtime().tm_year or not isinstance(poster_url, str) or not poster_url.startswith("https://"):
            continue
        movie_keys.add(key)
        movie["posterUrl"] = poster_url
        movies.append(movie)

    with FEATURED_CATALOG_PATH.open(encoding="utf-8") as featured_file:
        featured_catalog = json.load(featured_file)
    if not isinstance(featured_catalog, list):
        raise ValueError("The featured movie catalog must be a JSON array.")

    for featured_order, item in enumerate(featured_catalog):
        if not isinstance(item, dict):
            raise ValueError("Every featured movie must be an object.")
        title = item.get("title")
        release_date = item.get("releaseDate")
        year = item.get("year")
        if release_date is not None:
            if not isinstance(release_date, str):
                raise ValueError("Featured movie release dates must be ISO date strings.")
            try:
                parsed_release_date = date.fromisoformat(release_date)
            except ValueError as error:
                raise ValueError("Featured movie release dates must use YYYY-MM-DD.") from error
            year = parsed_release_date.year
        else:
            parsed_release_date = None
        language = item.get("language")
        country = item.get("country")
        if (
            not isinstance(title, str)
            or not title.strip()
            or not isinstance(year, int)
            or isinstance(year, bool)
            or not isinstance(language, str)
            or not language.strip()
            or not isinstance(country, str)
            or not country.strip()
        ):
            raise ValueError("Every featured movie needs a title, year, language, and country.")
        normalized_title = title.strip()
        key = (normalized_title.casefold(), year, language.strip().casefold())
        if key in movie_keys:
            continue
        poster_url = item.get("posterUrl")
        if not isinstance(poster_url, str) or not poster_url.startswith("https://"):
            continue
        movie_keys.add(key)
        industry = (
            INDUSTRY_BY_LANGUAGE.get(language.strip(), "International")
            if country.strip().casefold() == "india"
            else "Hollywood" if country.strip().casefold() == "usa"
            else "British cinema" if country.strip().casefold() == "uk"
            else "International"
        )
        movie = {
            "title": normalized_title,
            "year": year,
            "genre": "Unclassified",
            "language": language.strip(),
            "industry": industry,
            "country": country.strip(),
            "tag": "2026 pick",
            "style": DEFAULT_STYLE,
            "featuredOrder": featured_order,
        }
        if parsed_release_date:
            movie["releaseDate"] = parsed_release_date.isoformat()
        movie["posterUrl"] = poster_url
        movies.append(movie)

    if not movies:
        raise ValueError("The movie catalog cannot be empty.")
    return movies


MOVIES = load_movies()
MOVIES_BY_TITLE: dict[str, dict[str, Any]] = {}
for movie in MOVIES:
    MOVIES_BY_TITLE.setdefault(movie["title"], movie)


@contextmanager
def connect_database(database_path: str | Path) -> Iterator[sqlite3.Connection]:
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize_database(database_path: str | Path) -> None:
    with connect_database(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_salt BLOB NOT NULL,
                password_hash BLOB NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS sessions_user_id_idx ON sessions(user_id);
            CREATE TABLE IF NOT EXISTS watchlist (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                movie_title TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (user_id, movie_title)
            );
            """
        )
        connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (int(time.time()),))
        connection.commit()


def hash_password(password: str, salt: bytes | None = None) -> tuple[bytes, bytes]:
    actual_salt = salt or secrets.token_bytes(16)
    password_hash = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), actual_salt, PASSWORD_ITERATIONS
    )
    return actual_salt, password_hash


def make_session(
    connection: sqlite3.Connection, user_id: int
) -> str:
    token = secrets.token_urlsafe(32)
    connection.execute(
        "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
        (
            hashlib.sha256(token.encode("ascii")).hexdigest(),
            user_id,
            int(time.time()) + SESSION_LIFETIME_SECONDS,
        ),
    )
    return token


def filter_movies(
    movies: list[dict[str, Any]],
    *,
    query: str = "",
    year: int | None = None,
    genre: str = "All",
    industry: str = "All",
    saved_titles: set[str] | None = None,
) -> list[dict[str, Any]]:
    search_term = query.strip().casefold()
    results = []
    for movie in movies:
        if search_term and not search_term in " ".join(
            str(movie[field])
            for field in (
                "title",
                "genre",
                "year",
                "releaseDate",
                "language",
                "industry",
                "country",
            )
            if field in movie
        ).casefold():
            continue
        if year is not None and movie["year"] != year:
            continue
        if genre != "All" and movie["genre"].casefold() != genre.casefold():
            continue
        if industry != "All" and movie["industry"].casefold() != industry.casefold():
            continue
        if saved_titles is not None and movie["title"] not in saved_titles:
            continue
        results.append(movie)
    results.sort(
        key=lambda movie: (
            0 if "featuredOrder" in movie else 1,
            0 if movie.get("country") == "India" else 1,
            0 if movie.get("releaseDate") else 1,
            -date.fromisoformat(movie["releaseDate"]).toordinal()
            if movie.get("releaseDate")
            else -movie["year"],
            movie.get("featuredOrder", 0),
            movie["title"].casefold(),
        )
    )
    return results


class MoviesWizzHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        initialize_database(self.database_path)
        super().__init__(address, MoviesWizzRequestHandler)


class MoviesWizzRequestHandler(BaseHTTPRequestHandler):
    server: MoviesWizzHTTPServer
    protocol_version = "HTTP/1.1"

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("X-Frame-Options", "DENY")
        super().end_headers()

    def send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict[str, Any]:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("Invalid Content-Length header.") from error
        if content_length < 1 or content_length > MAX_REQUEST_BYTES:
            raise ValueError("Request body must be between 1 byte and 1 MB.")
        try:
            payload = json.loads(self.rfile.read(content_length))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ValueError("Request body must contain valid JSON.") from error
        if not isinstance(payload, dict):
            raise ValueError("Request body must be a JSON object.")
        return payload

    def request_user(
        self, connection: sqlite3.Connection
    ) -> sqlite3.Row | None:
        authorization = self.headers.get("Authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.casefold() != "bearer" or not token or len(token) > 256:
            return None
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        return connection.execute(
            """
            SELECT users.id, users.name, users.email
            FROM sessions JOIN users ON users.id = sessions.user_id
            WHERE sessions.token_hash = ? AND sessions.expires_at > ?
            """,
            (token_hash, int(time.time())),
        ).fetchone()

    def require_user(self, connection: sqlite3.Connection) -> sqlite3.Row:
        user = self.request_user(connection)
        if user is None:
            raise PermissionError("Sign in to access your account.")
        return user

    def handle_error(self, error: Exception) -> None:
        if isinstance(error, PermissionError):
            self.send_json(401, {"error": str(error)})
        elif isinstance(error, ValueError):
            self.send_json(400, {"error": str(error)})
        elif isinstance(error, LookupError):
            self.send_json(404, {"error": str(error)})
        else:
            LOGGER.exception("Request failed")
            self.send_json(500, {"error": "The server could not complete the request."})

    def do_GET(self) -> None:
        parsed_url = urlsplit(self.path)
        path = unquote(parsed_url.path)
        try:
            if path in ("/", "/index.html"):
                self.serve_index()
                return
            if path == "/api/health":
                self.send_json(200, {"status": "ok"})
                return
            if path == "/api/movies":
                self.get_movies(parse_qs(parsed_url.query))
                return
            if path.startswith("/api/movies/"):
                self.get_movie(path.removeprefix("/api/movies/"))
                return
            if path == "/api/auth/me":
                self.get_current_user()
                return
            if path == "/api/watchlist":
                self.get_watchlist()
                return
            self.send_json(404, {"error": "Endpoint not found."})
        except (PermissionError, ValueError, LookupError) as error:
            self.handle_error(error)
        except Exception as error:
            self.handle_error(error)

    def do_POST(self) -> None:
        path = unquote(urlsplit(self.path).path)
        try:
            if path == "/api/auth/register":
                self.register()
            elif path == "/api/auth/login":
                self.login()
            elif path == "/api/auth/logout":
                self.logout()
            elif path == "/api/watchlist":
                self.add_watchlist_titles()
            else:
                self.send_json(404, {"error": "Endpoint not found."})
        except (PermissionError, ValueError, LookupError) as error:
            self.handle_error(error)
        except Exception as error:
            self.handle_error(error)

    def do_DELETE(self) -> None:
        path = unquote(urlsplit(self.path).path)
        prefix = "/api/watchlist/"
        if not path.startswith(prefix):
            self.send_json(404, {"error": "Endpoint not found."})
            return
        try:
            title = path.removeprefix(prefix)
            if not title:
                raise ValueError("A movie title is required.")
            with connect_database(self.server.database_path) as connection:
                user = self.require_user(connection)
                connection.execute(
                    "DELETE FROM watchlist WHERE user_id = ? AND movie_title = ?",
                    (user["id"], title),
                )
                connection.commit()
                self.send_json(200, {"titles": self.watchlist_titles(connection, user["id"])})
        except (PermissionError, ValueError, LookupError) as error:
            self.handle_error(error)
        except Exception as error:
            self.handle_error(error)

    def serve_index(self) -> None:
        for index_path in (BACKEND_DIR / "index.html", APP_DIR / "index.html"):
            try:
                body = index_path.read_bytes()
                break
            except FileNotFoundError:
                continue
            except OSError as error:
                raise LookupError("The application page could not be loaded.") from error
        else:
            raise LookupError("The application page could not be loaded.")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def get_movies(self, parameters: dict[str, list[str]]) -> None:
        query = self.single_parameter(parameters, "q", "")
        if len(query) > 120:
            raise ValueError("Search text cannot exceed 120 characters.")
        year_text = self.single_parameter(parameters, "year", "")
        try:
            year = int(year_text) if year_text else None
        except ValueError as error:
            raise ValueError("Year must be a whole number.") from error
        genre = self.single_parameter(parameters, "genre", "All")
        industry = self.single_parameter(parameters, "industry", "All")
        saved_only = self.single_parameter(parameters, "saved", "false").casefold()
        if saved_only not in ("true", "false"):
            raise ValueError("The saved filter must be true or false.")
        try:
            limit = int(self.single_parameter(parameters, "limit", "1000"))
            offset = int(self.single_parameter(parameters, "offset", "0"))
        except ValueError as error:
            raise ValueError("Limit and offset must be whole numbers.") from error
        if not 1 <= limit <= 1000 or not 0 <= offset <= 100_000:
            raise ValueError("Limit must be 1–1000 and offset 0–100000.")

        saved_titles = None
        if saved_only == "true":
            with connect_database(self.server.database_path) as connection:
                user = self.require_user(connection)
                saved_titles = set(self.watchlist_titles(connection, user["id"]))
        results = filter_movies(
            MOVIES,
            query=query,
            year=year,
            genre=genre,
            industry=industry,
            saved_titles=saved_titles,
        )
        self.send_json(
            200,
            {
                "movies": results[offset : offset + limit],
                "total": len(results),
                "limit": limit,
                "offset": offset,
            },
        )

    def get_movie(self, title: str) -> None:
        movie = MOVIES_BY_TITLE.get(title)
        if movie is None:
            raise LookupError("Movie not found.")
        self.send_json(200, {"movie": movie})

    def get_current_user(self) -> None:
        with connect_database(self.server.database_path) as connection:
            user = self.require_user(connection)
            self.send_json(200, {"user": self.public_user(user)})

    def register(self) -> None:
        payload = self.read_json()
        name = payload.get("name")
        email = payload.get("email")
        password = payload.get("password")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            raise ValueError("Enter a name between 1 and 80 characters.")
        if not isinstance(email, str) or len(email.strip()) > 254:
            raise ValueError("Enter a valid email address.")
        normalized_email = email.strip().casefold()
        if not EMAIL_PATTERN.fullmatch(normalized_email):
            raise ValueError("Enter a valid email address.")
        if not isinstance(password, str) or not 8 <= len(password) <= 128:
            raise ValueError("Password must be between 8 and 128 characters.")

        salt, password_hash = hash_password(password)
        with connect_database(self.server.database_path) as connection:
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO users (name, email, password_salt, password_hash, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (name.strip(), normalized_email, salt, password_hash, int(time.time())),
                )
            except sqlite3.IntegrityError as error:
                raise ValueError("An account with this email already exists.") from error
            user_id = cursor.lastrowid
            token = make_session(connection, user_id)
            connection.commit()
            user = connection.execute(
                "SELECT id, name, email FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        self.send_json(201, {"token": token, "user": self.public_user(user)})

    def login(self) -> None:
        payload = self.read_json()
        email = payload.get("email")
        password = payload.get("password")
        if not isinstance(email, str) or not isinstance(password, str):
            raise ValueError("Email and password are required.")
        if len(email) > 254 or len(password) > 128:
            self.send_json(401, {"error": "Invalid email or password."})
            return
        normalized_email = email.strip().casefold()
        with connect_database(self.server.database_path) as connection:
            user_record = connection.execute(
                """
                SELECT id, name, email, password_salt, password_hash
                FROM users WHERE email = ?
                """,
                (normalized_email,),
            ).fetchone()
            if user_record is None:
                self.send_json(401, {"error": "Invalid email or password."})
                return
            _, candidate_hash = hash_password(password, user_record["password_salt"])
            if not hmac.compare_digest(candidate_hash, user_record["password_hash"]):
                self.send_json(401, {"error": "Invalid email or password."})
                return
            token = make_session(connection, user_record["id"])
            connection.commit()
            user = connection.execute(
                "SELECT id, name, email FROM users WHERE id = ?", (user_record["id"],)
            ).fetchone()
        self.send_json(200, {"token": token, "user": self.public_user(user)})

    def logout(self) -> None:
        authorization = self.headers.get("Authorization", "")
        scheme, _, token = authorization.partition(" ")
        with connect_database(self.server.database_path) as connection:
            if scheme.casefold() == "bearer" and token and len(token) <= 256:
                token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
                connection.execute(
                    "DELETE FROM sessions WHERE token_hash = ?", (token_hash,)
                )
                connection.commit()
        self.send_json(200, {"status": "signed out"})

    def get_watchlist(self) -> None:
        with connect_database(self.server.database_path) as connection:
            user = self.require_user(connection)
            self.send_json(
                200, {"titles": self.watchlist_titles(connection, user["id"])}
            )

    def add_watchlist_titles(self) -> None:
        payload = self.read_json()
        titles = payload.get("titles")
        if (
            not isinstance(titles, list)
            or len(titles) > 1000
            or any(not isinstance(title, str) for title in titles)
        ):
            raise ValueError("Titles must be a list of up to 1000 movie names.")
        unknown_titles = sorted(set(titles) - MOVIES_BY_TITLE.keys())
        if unknown_titles:
            raise ValueError(f"Movie not found in catalog: {unknown_titles[0]}")
        with connect_database(self.server.database_path) as connection:
            user = self.require_user(connection)
            connection.executemany(
                """
                INSERT OR IGNORE INTO watchlist (user_id, movie_title, created_at)
                VALUES (?, ?, ?)
                """,
                [(user["id"], title, int(time.time())) for title in set(titles)],
            )
            connection.commit()
            self.send_json(
                200, {"titles": self.watchlist_titles(connection, user["id"])}
            )

    @staticmethod
    def watchlist_titles(connection: sqlite3.Connection, user_id: int) -> list[str]:
        rows = connection.execute(
            """
            SELECT movie_title FROM watchlist
            WHERE user_id = ?
            ORDER BY movie_title COLLATE NOCASE
            """,
            (user_id,),
        ).fetchall()
        return [row["movie_title"] for row in rows]

    @staticmethod
    def public_user(user: sqlite3.Row) -> dict[str, Any]:
        return {"id": user["id"], "name": user["name"], "email": user["email"]}

    @staticmethod
    def single_parameter(
        parameters: dict[str, list[str]], name: str, default: str
    ) -> str:
        values = parameters.get(name)
        if values is None:
            return default
        if len(values) != 1:
            raise ValueError(f"Parameter {name} must be specified once.")
        return values[0]

    def log_message(self, format_string: str, *args: Any) -> None:
        LOGGER.info("%s - %s", self.address_string(), format_string % args)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    server = MoviesWizzHTTPServer(("127.0.0.1", 8000), DATABASE_PATH)
    LOGGER.info("Movies Wizz is available at http://127.0.0.1:8000")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("Shutting down Movies Wizz.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
