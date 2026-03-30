import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import urljoin

import requests
import tweepy
from bs4 import BeautifulSoup

SOURCE_URL = os.getenv("SOURCE_URL", "https://hochi.news/mlb/?kd_page=top")
STATE_PATH = Path(os.getenv("STATE_PATH", "state.json"))
USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (compatible; mlb-x-bot/1.0; +https://github.com/)"
)
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "25"))
MAX_GAMES = int(os.getenv("MAX_GAMES", "6"))
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


@dataclass
class GameSummary:
    game_id: str
    url: str
    title: str
    inning_labels: List[str]
    teams: List[Tuple[str, List[str]]]
    result_text: str

    def fingerprint(self) -> str:
        payload = {
            "inning_labels": self.inning_labels,
            "teams": self.teams,
            "result_text": self.result_text,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def load_state(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {"posted": {}, "last_run_utc": ""}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.warning("state.json is invalid. Recreating it.")
        return {"posted": {}, "last_run_utc": ""}


def save_state(path: Path, state: Dict[str, str]) -> None:
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def http_get(url: str) -> str:
    headers = {"User-Agent": USER_AGENT}
    last_error: Optional[Exception] = None
    for attempt in range(1, 4):
        try:
            response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            response.encoding = response.apparent_encoding or response.encoding
            return response.text
        except requests.RequestException as error:
            last_error = error
            sleep_sec = attempt * 2
            logger.warning("GET failed (%s). retry in %ss: %s", attempt, sleep_sec, url)
            time.sleep(sleep_sec)
    raise RuntimeError(f"Failed to fetch {url}: {last_error}")


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def discover_game_links(top_html: str, base_url: str) -> List[str]:
    soup = BeautifulSoup(top_html, "html.parser")
    links: List[str] = []
    seen = set()

    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "")
        if "kd_page=game" not in href and "global_id" not in href:
            continue
        full_url = urljoin(base_url, href)
        if full_url in seen:
            continue
        seen.add(full_url)
        links.append(full_url)

    # fallback: capture game links from inline scripts
    if not links:
        for hit in re.findall(r'https?://hochi\.news/mlb/\?kd_page=game[^"\']+', top_html):
            if hit not in seen:
                seen.add(hit)
                links.append(hit)

    return links[:MAX_GAMES]


def parse_score_table(soup: BeautifulSoup) -> Tuple[List[str], List[Tuple[str, List[str]]]]:
    candidate_tables = soup.select("table")
    for table in candidate_tables:
        headers = [normalize_space(th.get_text(" ")) for th in table.select("tr th")]
        if not headers:
            continue

        inning_labels = [h for h in headers if re.match(r"^(\d+|R|H|E)$", h, re.IGNORECASE)]
        if len(inning_labels) < 3:
            continue

        teams: List[Tuple[str, List[str]]] = []
        for row in table.select("tr"):
            cells = row.find_all(["th", "td"])
            if len(cells) < len(inning_labels) + 1:
                continue
            team_name = normalize_space(cells[0].get_text(" "))
            scores = [normalize_space(c.get_text(" ")) for c in cells[1 : len(inning_labels) + 1]]
            if team_name and any(score for score in scores):
                teams.append((team_name, scores))

        if len(teams) >= 2:
            return inning_labels, teams[:2]

    return [], []


def parse_result_text(soup: BeautifulSoup) -> str:
    candidates = [
        ".score__result",
        ".gameResult",
        ".result",
        "h1",
        "title",
    ]
    for selector in candidates:
        node = soup.select_one(selector)
        if not node:
            continue
        text = normalize_space(node.get_text(" "))
        if len(text) >= 6:
            return text
    return "試合結果"


def extract_game_id(url: str, soup: BeautifulSoup) -> str:
    m = re.search(r"global_id=([0-9]+)", url)
    if m:
        return m.group(1)
    canonical = soup.find("link", rel="canonical")
    if canonical and canonical.get("href"):
        m = re.search(r"global_id=([0-9]+)", canonical["href"])
        if m:
            return m.group(1)
    return hashlib.md5(url.encode("utf-8")).hexdigest()[:12]


def parse_game(url: str) -> Optional[GameSummary]:
    html = http_get(url)
    soup = BeautifulSoup(html, "html.parser")

    title_node = soup.select_one("h1") or soup.select_one("title")
    title = normalize_space(title_node.get_text(" ")) if title_node else "MLB速報"

    inning_labels, teams = parse_score_table(soup)
    if not inning_labels or len(teams) < 2:
        logger.info("No inning table found: %s", url)
        return None

    result_text = parse_result_text(soup)
    game_id = extract_game_id(url, soup)
    return GameSummary(
        game_id=game_id,
        url=url,
        title=title,
        inning_labels=inning_labels,
        teams=teams,
        result_text=result_text,
    )


def split_post(text: str, max_chars: int = 275) -> List[str]:
    lines = text.split("\n")
    chunks: List[str] = []
    current = ""
    for line in lines:
        candidate = f"{current}\n{line}".strip() if current else line
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            chunks.append(current)
        current = line

    if current:
        chunks.append(current)

    return chunks


def build_posts(game: GameSummary) -> List[str]:
    head = f"⚾ {game.result_text}\n{game.title}\n{game.url}"

    rows = []
    inning_header = " ".join(game.inning_labels)
    rows.append(f"[Inning] {inning_header}")
    for team_name, scores in game.teams:
        rows.append(f"{team_name}: {' '.join(scores)}")

    body = "\n".join(rows)
    return split_post(f"{head}\n\n{body}")


def create_client() -> tweepy.Client:
    required = [
        "X_API_KEY",
        "X_API_SECRET",
        "X_ACCESS_TOKEN",
        "X_ACCESS_TOKEN_SECRET",
    ]
    missing = [name for name in required if not os.getenv(name)]
    if missing and not DRY_RUN:
        raise RuntimeError(f"Missing env vars: {', '.join(missing)}")

    return tweepy.Client(
        consumer_key=os.getenv("X_API_KEY"),
        consumer_secret=os.getenv("X_API_SECRET"),
        access_token=os.getenv("X_ACCESS_TOKEN"),
        access_token_secret=os.getenv("X_ACCESS_TOKEN_SECRET"),
    )


def post_thread(client: tweepy.Client, posts: Sequence[str]) -> List[str]:
    ids: List[str] = []
    reply_to: Optional[str] = None

    for idx, post in enumerate(posts, start=1):
        if DRY_RUN:
            logger.info("DRY_RUN post %s/%s:\n%s", idx, len(posts), post)
            fake_id = f"dry-run-{idx}"
            ids.append(fake_id)
            reply_to = fake_id
            continue

        response = client.create_tweet(
            text=post,
            in_reply_to_tweet_id=reply_to,
        )
        tweet_id = str(response.data["id"])
        ids.append(tweet_id)
        reply_to = tweet_id
        time.sleep(1.5)

    return ids


def run() -> int:
    state = load_state(STATE_PATH)
    posted: Dict[str, str] = state.setdefault("posted", {})

    logger.info("Fetching top page: %s", SOURCE_URL)
    top_html = http_get(SOURCE_URL)
    game_links = discover_game_links(top_html, SOURCE_URL)
    if not game_links:
        logger.warning("No game links found on top page.")
        state["last_run_utc"] = datetime.now(timezone.utc).isoformat()
        save_state(STATE_PATH, state)
        return 0

    client = create_client()
    posted_count = 0

    for url in game_links:
        try:
            game = parse_game(url)
        except Exception:
            logger.exception("Failed to parse game page: %s", url)
            continue

        if not game:
            continue

        digest = game.fingerprint()
        previous = posted.get(game.game_id)
        if previous == digest:
            logger.info("Already posted game_id=%s (unchanged)", game.game_id)
            continue

        posts = build_posts(game)
        logger.info("Posting %s tweet(s) for game_id=%s", len(posts), game.game_id)

        try:
            post_thread(client, posts)
        except Exception:
            logger.exception("Posting failed for game_id=%s", game.game_id)
            continue

        posted[game.game_id] = digest
        posted_count += 1

    state["last_run_utc"] = datetime.now(timezone.utc).isoformat()
    save_state(STATE_PATH, state)
    logger.info("Done. posted_count=%s", posted_count)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
