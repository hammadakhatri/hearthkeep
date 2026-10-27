"""The Analysis gallery: every attached file and every chart, grouped by chat."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from hearthkeep.api.common import ApiError, current_session, services
from hearthkeep.auth.sessions import Session

router = APIRouter(prefix="/api")

SessionDep = Annotated[Session, Depends(current_session)]

DEFAULT_LIMIT = 20
MAX_LIMIT = 50
# A pending file is still being inspected: it joins the gallery once it is ready or unchecked.
_SHOWN = "('ready', 'unchecked')"

def _fixed(column: str) -> str:
    """SQL: the stored time in fixed width (older rows have no fraction: `...:00+00:00`), so
    string order is time order and agrees with the normalised cursor."""
    return (
        f"CASE WHEN instr({column}, '.') = 0"
        f" THEN substr({column}, 1, 19) || '.000000' || substr({column}, 20) ELSE {column} END"
    )


# A chat is listed when it has a shown file; it is as new as its newest shown file or run.
# The page cursor is (updated_at, id): chats with the same time are ordered by id, so a page
# edge between them skips none.
_CHATS = f"""
SELECT id, title, updated_at FROM (
    SELECT c.id AS id, c.title AS title,
           MAX(f.at, COALESCE(r.at, f.at)) AS updated_at
    FROM chats c
    JOIN (SELECT chat_id, MAX({_fixed("created_at")}) AS at FROM chat_files
          WHERE status IN {_SHOWN} GROUP BY chat_id) f ON f.chat_id = c.id
    LEFT JOIN (SELECT chat_id, MAX({_fixed("created_at")}) AS at FROM runs
               GROUP BY chat_id) r ON r.chat_id = c.id
)
WHERE (? IS NULL OR updated_at < ? OR (updated_at = ? AND ? IS NOT NULL AND id < ?))
ORDER BY updated_at DESC, id DESC
LIMIT ?
"""


def _before(value: str | None) -> str | None:
    """The cursor in the stored form (UTC ISO-8601, microseconds); 422 when it isn't a time."""
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ApiError(422, "invalid_request", message="`before` must be a time.") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat(timespec="microseconds")


@router.get("/analysis")
async def analysis(
    session: SessionDep,
    request: Request,
    before: Annotated[str | None, Query(max_length=64)] = None,
    before_id: Annotated[str | None, Query(pattern="^[0-9a-f]{32}$")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> dict:
    """A page of chats (newest first) with their files and chart titles: three bounded
    queries however many chats there are. A chart's spec is fetched on its own
    (`/api/outputs/{id}`), so this stays small. The next page is `before` = the last chat's
    `updated_at` and `before_id` = its `chat_id`."""
    conn = services(request).conn
    cursor = _before(before)
    # One more than asked for tells whether there is another page.
    after_id = before_id if cursor is not None else None
    chats = conn.execute(
        _CHATS, (cursor, cursor, cursor, after_id, after_id, limit + 1)
    ).fetchall()
    more = len(chats) > limit
    chats = chats[:limit]
    if not chats:
        return {"chats": [], "more": False}
    ids = [c["id"] for c in chats]
    marks = ",".join("?" * len(ids))
    files: dict[str, list[dict]] = {i: [] for i in ids}
    for row in conn.execute(
        "SELECT id, chat_id, name, kind, size, status, created_at FROM chat_files"
        f" WHERE chat_id IN ({marks}) AND status IN {_SHOWN} ORDER BY created_at, rowid",
        ids,
    ):
        files[row["chat_id"]].append(dict(row))
    charts: dict[str, list[dict]] = {i: [] for i in ids}
    for row in conn.execute(
        "SELECT id, run_id, chat_id, name, created_at FROM outputs"
        f" WHERE chat_id IN ({marks}) AND kind = 'chart' ORDER BY created_at, rowid",
        ids,
    ):
        charts[row["chat_id"]].append(
            {"id": row["id"], "run_id": row["run_id"], "title": row["name"],
             "created_at": row["created_at"]}  # fmt: skip
        )
    return {
        "more": more,
        "chats": [
            {
                "chat_id": c["id"],
                "title": c["title"] or "",
                "updated_at": c["updated_at"],
                "files": files[c["id"]],
                "charts": charts[c["id"]],
            }
            for c in chats
        ]
    }
