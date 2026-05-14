"""
Chub Chat Exporter

Mass-export your own Chub chats. It primes Chub's own Export Chat request once
through the visible UI, then uses the same full-chat API request for the rest.
"""

import argparse
import csv
import json
import os
import random
import re
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


SCRIPT_DIR = Path(__file__).resolve().parent
PROFILE_DIR = SCRIPT_DIR / "chub_browser_profile"
EXPORT_DIR = SCRIPT_DIR / "chat_exports"
LINKS_FILE = SCRIPT_DIR / "chat_links.txt"
MANIFEST_FILE = SCRIPT_DIR / "export_manifest.csv"
FAILED_FILE = SCRIPT_DIR / "failed_exports.csv"
LOG_FILE = SCRIPT_DIR / "chub_chat_exporter.log"

CHUB_HOME = "https://chub.ai/"
MY_CHATS_URL = "https://chub.ai/my_chats"

DEFAULT_SCROLLS = 240
SCROLL_PAUSE_SECONDS = 0.65
FAST_API_DELAY_SECONDS = 0.05
GATEWAY_CHAT_EXPORT_URL = "https://gateway.chub.ai/api/core/chats/v2/{chat_id}"
CHUB_CARD_PNG_URL = "https://avatars.charhub.io/avatars/{full_path}/chara_card_v2.png"


def log(message):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    print(line)
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def ensure_dirs():
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)


def sanitize_filename(value, fallback="chat"):
    value = re.sub(r"[<>:\"/\\|?*\x00-\x1f]+", "_", value or "").strip()
    value = re.sub(r"\s+", " ", value).strip(" .")
    return (value or fallback)[:180]


def unique_path(directory, filename):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / sanitize_filename(filename)
    if not path.exists():
        return path

    stem = path.stem
    suffix = path.suffix
    for index in range(2, 10000):
        candidate = directory / f"{stem} ({index}){suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not make a unique filename for {filename}")


def numbered_chat_path(character_name):
    directory = EXPORT_DIR
    directory.mkdir(parents=True, exist_ok=True)
    safe_name = sanitize_filename(character_name, fallback="unknown-character")
    for index in range(100000):
        path = directory / f"{safe_name}-chat-{index:03d}.jsonl"
        if not path.exists():
            return path
    raise RuntimeError(f"Could not make a unique chat filename for {character_name}")


def is_current_chat_export_path(path):
    return bool(re.fullmatch(r".+-chat-\d{3,}\.jsonl", path.name, flags=re.IGNORECASE))


def chat_id_from_url(chat_url):
    match = re.search(r"/chats/([^/?#]+)", chat_url)
    if match:
        return match.group(1)
    return sanitize_filename(chat_url.rstrip("/").rsplit("/", 1)[-1], fallback="chat")


def full_chat_api_url(chat_url):
    chat_id = chat_id_from_url(chat_url)
    query = (
        f"nocache={random.random()}"
        "&include_messages=true"
        "&include_config=true"
        "&include_meta=true"
    )
    return f"{GATEWAY_CHAT_EXPORT_URL.format(chat_id=chat_id)}?{query}"


def is_full_chat_export_request(url, chat_url):
    chat_id = chat_id_from_url(chat_url)
    return (
        f"/api/core/chats/v2/{chat_id}" in url
        and "include_messages=true" in url
        and "include_config=true" in url
        and "include_meta=true" in url
    )


def append_csv(path, fieldnames, row):
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def load_done_urls(require_bot_png=False):
    if not MANIFEST_FILE.exists():
        return set()
    with MANIFEST_FILE.open("r", newline="", encoding="utf-8") as handle:
        done = set()
        for row in csv.DictReader(handle):
            chat_url = row.get("chat_url", "")
            saved_path = row.get("saved_path", "")
            saved = Path(saved_path) if saved_path else None
            if (
                chat_url
                and saved
                and saved.suffix.lower() == ".jsonl"
                and is_current_chat_export_path(saved)
                and saved.exists()
            ):
                if require_bot_png and not bot_png_path_for_chat_path(saved).exists():
                    continue
                done.add(chat_url)
        return done


def load_existing_jsonl_paths():
    if not MANIFEST_FILE.exists():
        return {}
    with MANIFEST_FILE.open("r", newline="", encoding="utf-8") as handle:
        existing = {}
        for row in csv.DictReader(handle):
            chat_url = row.get("chat_url", "")
            saved_path = row.get("saved_path", "")
            saved = Path(saved_path) if saved_path else None
            if (
                chat_url
                and saved
                and saved.suffix.lower() == ".jsonl"
                and is_current_chat_export_path(saved)
                and saved.exists()
            ):
                existing[chat_url] = saved
        return existing


def find_chrome_path():
    env_path = os.environ.get("CHROME_PATH")
    if env_path and Path(env_path).exists():
        return env_path

    candidates = [
        Path(os.environ.get("ProgramFiles", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LocalAppData", "")) / "Google/Chrome/Application/chrome.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


@contextmanager
def browser_context(playwright):
    ensure_dirs()
    chrome_path = find_chrome_path()
    launch_kwargs = {
        "user_data_dir": str(PROFILE_DIR),
        "headless": False,
        "accept_downloads": True,
        "downloads_path": str(EXPORT_DIR),
        "viewport": {"width": 1440, "height": 1000},
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
        ],
    }
    if chrome_path:
        launch_kwargs["executable_path"] = chrome_path
    else:
        launch_kwargs["channel"] = "chrome"

    context = playwright.chromium.launch_persistent_context(**launch_kwargs)
    try:
        yield context
    finally:
        context.close()


def first_page(context):
    if context.pages:
        return context.pages[0]
    return context.new_page()


def wait_for_page_settle(page, timeout=45000):
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except PlaywrightTimeoutError:
        log("Page kept background requests open; continuing anyway.")


def run_login():
    with sync_playwright() as playwright:
        with browser_context(playwright) as context:
            page = first_page(context)
            page.goto(CHUB_HOME, wait_until="domcontentloaded", timeout=60000)
            print()
            print("Log in to Chub in the Chrome window.")
            print("When the page is logged in and ready, come back here and press Enter.")
            input()
            log(f"Saved Chub browser profile at {PROFILE_DIR}")


def normalize_url(raw_url):
    if not raw_url:
        return ""
    parsed = urlparse(raw_url)
    if not parsed.netloc:
        return ""
    if not parsed.netloc.endswith("chub.ai"):
        return ""
    return raw_url.split("#", 1)[0]


def is_probable_chat_url(raw_url):
    url = normalize_url(raw_url)
    if not url:
        return False

    parsed = urlparse(url)
    path = parsed.path.lower().rstrip("/")
    query = parsed.query.lower()

    if path in ("", "/my_chats", "/login", "/register"):
        return False
    if "/my_chats" in path:
        return False
    if any(part in path for part in ("/assets/", "/favicon/", "/cdn-cgi/")):
        return False

    if path.startswith("/chat/") or path.startswith("/chats/"):
        return True
    if path.startswith("/characters/") and any(key in query for key in ("chat", "chatid", "chat_id", "history")):
        return True
    if "/chat/" in path or "/chats/" in path:
        return True
    return False


def collect_visible_chat_links(page):
    anchors = page.evaluate(
        """
        () => Array.from(document.querySelectorAll('a[href]')).map((a) => ({
            href: a.href,
            text: (a.innerText || a.textContent || '').trim(),
            aria: a.getAttribute('aria-label') || '',
            title: a.getAttribute('title') || ''
        }))
        """
    )
    links = []
    for anchor in anchors:
        href = normalize_url(anchor.get("href", ""))
        if href and is_probable_chat_url(href):
            links.append(href)
    return links


def click_show_more_if_visible(page):
    locators = [
        page.get_by_role("button", name=re.compile(r"show\s+more", re.I)),
        page.get_by_text(re.compile(r"show\s+more", re.I)),
        page.locator("button:has-text('Show More')"),
        page.locator("button:has-text('Show more')"),
    ]
    return click_one(page, locators, "Show more", timeout=1200)


def collect_chat_links(page, max_scrolls=DEFAULT_SCROLLS):
    log("Opening my_chats...")
    page.goto(MY_CHATS_URL, wait_until="domcontentloaded", timeout=90000)
    wait_for_page_settle(page)

    found = []
    seen = set()
    stable_rounds = 0
    last_count = 0

    for scroll_index in range(max_scrolls):
        for link in collect_visible_chat_links(page):
            if link in seen:
                continue
            seen.add(link)
            found.append(link)
            log(f"Found chat link {len(found)}: {link}")

        clicked_show_more = False
        while click_show_more_if_visible(page):
            clicked_show_more = True
            log("Clicked Show more.")
            time.sleep(SCROLL_PAUSE_SECONDS)
            for link in collect_visible_chat_links(page):
                if link in seen:
                    continue
                seen.add(link)
                found.append(link)
                log(f"Found chat link {len(found)}: {link}")

        if len(found) == last_count:
            stable_rounds += 1
        else:
            stable_rounds = 0
            last_count = len(found)

        if stable_rounds >= 8 and not clicked_show_more:
            log("No new chat links appeared after several scrolls.")
            break

        page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(SCROLL_PAUSE_SECONDS)

    LINKS_FILE.write_text("\n".join(found) + ("\n" if found else ""), encoding="utf-8")
    log(f"Saved {len(found)} chat link(s) to {LINKS_FILE}")
    return found


def click_one(page, locators, description, timeout=2500):
    last_error = None
    for locator in locators:
        try:
            count = locator.count()
            if count < 1:
                continue
            target = locator.first
            if not target.is_visible(timeout=timeout):
                continue
            target.click(timeout=timeout)
            return True
        except Exception as exc:
            last_error = exc
            continue
    if last_error:
        log(f"Could not click {description}: {last_error}")
    return False


def first_clickable(locators, timeout=2500):
    last_error = None
    for locator in locators:
        try:
            count = locator.count()
            if count < 1:
                continue
            target = locator.first
            if not target.is_visible(timeout=timeout):
                continue
            return target
        except Exception as exc:
            last_error = exc
            continue
    if last_error:
        log(f"Locator check failed: {last_error}")
    return None


def download_button_locators(page):
    return [
        page.get_by_role("button", name=re.compile(r"download\s+chat", re.I)),
        page.get_by_text(re.compile(r"download\s+chat", re.I)),
        page.locator("button:has-text('Download Chat')"),
        page.locator("[role='button']:has-text('Download Chat')"),
    ]


def export_button_locators(page):
    return [
        page.locator("[role='dialog']:has-text('Export Chat') button:has-text('Export')"),
        page.get_by_role("button", name=re.compile(r"^export$", re.I)),
        page.locator("button:has-text('Export')"),
    ]


def export_format_locators(page, label="JSONL"):
    return [
        page.locator(f"[role='dialog']:has-text('Export Chat') label:has-text('{label}')"),
        page.locator(f"[role='dialog']:has-text('Export Chat') .ant-radio-wrapper:has-text('{label}')"),
        page.get_by_text(label, exact=True),
    ]


def menu_button_locators(page):
    return [
        page.get_by_role("button", name=re.compile(r"menu", re.I)),
        page.locator("button[aria-label*='more' i]"),
        page.locator("[role='button'][aria-label*='more' i]"),
        page.locator("button[aria-label*='menu' i]"),
        page.locator("[role='button'][aria-label*='menu' i]"),
        page.locator("button[title*='menu' i]"),
        page.locator("[role='button'][title*='menu' i]"),
    ]


def chat_settings_locators(page):
    return [
        page.get_by_role("menuitem", name=re.compile(r"chat\s+settings", re.I)),
        page.get_by_role("button", name=re.compile(r"chat\s+settings", re.I)),
        page.get_by_text(re.compile(r"chat\s+settings", re.I)),
        page.locator("button:has-text('Chat Settings')"),
        page.locator("[role='button']:has-text('Chat Settings')"),
        page.locator("[role='menuitem']:has-text('Chat Settings')"),
    ]


def click_top_right_menu(page):
    if click_one(page, menu_button_locators(page), "top-right menu", timeout=1500):
        return True

    handle = page.evaluate_handle(
        """
        () => {
            const candidates = Array.from(document.querySelectorAll('button,[role="button"]'))
                .map((el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return {
                        el,
                        rect,
                        visible: rect.width > 8 && rect.height > 8 &&
                            style.visibility !== 'hidden' &&
                            style.display !== 'none' &&
                            rect.top >= 0 &&
                            rect.left >= window.innerWidth * 0.65 &&
                            rect.top <= 140
                    };
                })
                .filter((item) => item.visible)
                .sort((a, b) => (b.rect.left - a.rect.left) || (a.rect.top - b.rect.top));
            return candidates[0]?.el || null;
        }
        """
    )
    element = handle.as_element()
    if not element:
        return False

    box = element.bounding_box()
    if not box:
        return False
    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    return True


def export_one_chat(page, chat_url, include_bot_png=False, target_path=None):
    log(f"Opening chat: {chat_url}")
    page.goto(chat_url, wait_until="domcontentloaded", timeout=90000)
    wait_for_page_settle(page)
    time.sleep(1.0)

    saved_path = click_download_and_save(
        page,
        chat_url,
        timeout=1200,
        include_bot_png=include_bot_png,
        target_path=target_path,
    )
    if saved_path:
        return saved_path

    log("Download button not visible yet; opening top-right menu.")
    if not click_top_right_menu(page):
        raise RuntimeError("Could not find the top-right menu button.")

    time.sleep(0.5)
    if not click_one(page, chat_settings_locators(page), "Chat Settings", timeout=3500):
        raise RuntimeError("Could not find Chat Settings in the top-right menu.")

    time.sleep(0.8)
    saved_path = click_download_and_save(
        page,
        chat_url,
        timeout=3500,
        include_bot_png=include_bot_png,
        target_path=target_path,
    )
    if not saved_path:
        raise RuntimeError("Could not find Download Chat button after opening Chat Settings.")
    return saved_path


def click_download_and_save(page, chat_url, timeout=2500, include_bot_png=False, target_path=None):
    target = first_clickable(download_button_locators(page), timeout=timeout)
    if not target:
        return None

    try:
        with page.expect_download(timeout=2000) as download_info:
            target.click(timeout=timeout)
        download = download_info.value
    except PlaywrightTimeoutError as exc:
        # Chub currently opens an "Export Chat" modal after Download Chat.
        # The actual file starts after pressing Export in that modal.
        click_one(page, export_format_locators(page, "JSONL"), "JSONL export format", timeout=1500)
        export_target = first_clickable(export_button_locators(page), timeout=3500)
        if not export_target:
            raise RuntimeError("Download did not start and the Export Chat modal did not appear.") from exc
        try:
            chat_id = chat_id_from_url(chat_url)
            response_match = lambda response: (
                f"/api/core/chats/v2/{chat_id}" in response.url
                and "include_messages=true" in response.url
                and "include_config=true" in response.url
                and "include_meta=true" in response.url
            )
            with page.expect_response(response_match, timeout=30000) as response_info:
                export_target.click(timeout=3500)
            response = response_info.value
            if not response.ok:
                raise RuntimeError(f"Full chat export request failed: HTTP {response.status}")
            return save_response_export(
                response,
                chat_url,
                request_context=page.context.request,
                include_bot_png=include_bot_png,
                target_path=target_path,
            )
        except PlaywrightTimeoutError as export_exc:
            raise RuntimeError("Export modal appeared, but the full-chat response did not arrive.") from export_exc

    suggested = download.suggested_filename or "chub_chat.jsonl"
    saved_path = unique_path(EXPORT_DIR, suggested)
    download.save_as(str(saved_path))
    log(f"Saved: {saved_path.name}")
    if include_bot_png:
        log("Bot PNG skipped for direct browser download because no chat payload was available.")

    append_csv(
        MANIFEST_FILE,
        ["downloaded_at", "chat_url", "filename", "saved_path"],
        {
            "downloaded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "chat_url": chat_url,
            "filename": saved_path.name,
            "saved_path": str(saved_path),
        },
    )
    return saved_path


def save_response_export(
    response,
    chat_url,
    request_context=None,
    request_headers=None,
    include_bot_png=False,
    target_path=None,
):
    try:
        return save_payload_export(
            response.json(),
            chat_url,
            request_context=request_context,
            request_headers=request_headers,
            include_bot_png=include_bot_png,
            target_path=target_path,
        )
    except Exception:
        chat_id = chat_id_from_url(chat_url)
        saved_path = unique_path(EXPORT_DIR, f"chub_chat_{chat_id}.txt")
        saved_path.write_text(response.text(), encoding="utf-8")

    log(f"Saved: {saved_path.name}")
    append_csv(
        MANIFEST_FILE,
        ["downloaded_at", "chat_url", "filename", "saved_path"],
        {
            "downloaded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "chat_url": chat_url,
            "filename": saved_path.name,
            "saved_path": str(saved_path),
        },
    )
    return saved_path


def save_payload_export(
    payload,
    chat_url,
    request_context=None,
    request_headers=None,
    include_bot_png=False,
    target_path=None,
):
    character_name = character_name_from_payload(payload)
    saved_path = Path(target_path) if target_path else numbered_chat_path(character_name)
    saved_path.parent.mkdir(parents=True, exist_ok=True)
    write_jsonl_payload(saved_path, payload)
    if include_bot_png:
        try:
            download_bot_png_for_payload(payload, saved_path, request_context, request_headers)
        except Exception as exc:
            log(f"Bot PNG skipped for {saved_path.name}: {exc}")

    log(f"Saved: {saved_path.name}")
    append_csv(
        MANIFEST_FILE,
        ["downloaded_at", "chat_url", "filename", "saved_path"],
        {
            "downloaded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "chat_url": chat_url,
            "filename": saved_path.name,
            "saved_path": str(saved_path),
        },
    )
    return saved_path


def first_nonempty(*values):
    for value in values:
        if value not in (None, ""):
            return value
    return ""


def compact_dict(value):
    cleaned = {}
    for key, item in value.items():
        if isinstance(item, dict):
            item = compact_dict(item)
        elif isinstance(item, list):
            item = [compact_dict(entry) if isinstance(entry, dict) else entry for entry in item]

        if item in (None, "", [], {}):
            continue
        cleaned[key] = item
    return cleaned


def entity_display_name(entity):
    if not isinstance(entity, dict):
        return ""
    for key in ("name", "username", "display_name", "displayName", "full_path", "fullPath", "title"):
        value = entity.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().rsplit("/", 1)[-1]
    return ""


def entity_map_from_payload(payload, key):
    if not isinstance(payload, dict):
        return {}

    chat = payload.get("chat") if isinstance(payload.get("chat"), dict) else {}
    source = chat.get(key) or payload.get(key) or {}
    entities = {}

    if isinstance(source, dict):
        items = source.items()
    elif isinstance(source, list):
        items = ((item.get("id", index), item) for index, item in enumerate(source) if isinstance(item, dict))
    else:
        items = []

    for entity_id, entity in items:
        if not isinstance(entity, dict):
            continue
        entities[str(entity_id)] = entity
        for id_key in ("id", "user_id", "character_id"):
            if entity.get(id_key) not in (None, ""):
                entities[str(entity[id_key])] = entity
    return entities


def user_name_from_payload(payload):
    users = entity_map_from_payload(payload, "users")
    for entity in users.values():
        name = entity_display_name(entity)
        if name:
            return name
    return "You"


def chat_messages_from_payload(payload):
    if not isinstance(payload, dict):
        return []
    source = (
        payload.get("chatMessages")
        or payload.get("chat_messages")
        or payload.get("messages")
        or []
    )
    if isinstance(source, dict):
        return [message for message in source.values() if isinstance(message, dict)]
    if isinstance(source, list):
        return [message for message in source if isinstance(message, dict)]
    return []


def message_order_key(message):
    level = message.get("level")
    try:
        level = int(level)
    except (TypeError, ValueError):
        level = 0
    return (
        str(first_nonempty(message.get("created_at"), message.get("createdAt"))),
        level,
        str(first_nonempty(message.get("id"), "")),
    )


def ordered_chat_messages(payload):
    return sorted(chat_messages_from_payload(payload), key=message_order_key)


def message_speaker_name(payload, message, character_name, user_name):
    originator_name = entity_display_name(message.get("originator"))
    if originator_name:
        return originator_name

    speaker_id = str(first_nonempty(message.get("speaker_id"), message.get("speakerId")))
    is_bot = bool(first_nonempty(message.get("is_bot"), message.get("isBot")))

    if is_bot:
        character = entity_map_from_payload(payload, "characters").get(speaker_id)
        return entity_display_name(character) or character_name

    user = entity_map_from_payload(payload, "users").get(speaker_id)
    return entity_display_name(user) or user_name or "You"


def payload_to_jsonl_rows(payload):
    chat = payload.get("chat") if isinstance(payload, dict) and isinstance(payload.get("chat"), dict) else {}
    character_name = character_name_from_payload(payload)
    user_name = user_name_from_payload(payload)
    rows = [
        {
            "user_name": user_name,
            "character_name": character_name,
            "create_date": str(first_nonempty(chat.get("created_at"), chat.get("createdAt"))),
            "chat_metadata": compact_dict(
                {
                    "source": "chub.ai",
                    "chat_id": first_nonempty(chat.get("id"), payload.get("id") if isinstance(payload, dict) else ""),
                    "chat_name": chat.get("name"),
                    "summary": chat.get("summary"),
                }
            ),
        }
    ]

    for message in ordered_chat_messages(payload):
        text = str(first_nonempty(message.get("message"), message.get("mes"), message.get("content")))
        is_bot = bool(first_nonempty(message.get("is_bot"), message.get("isBot")))
        extra = {}
        extensions = message.get("extensions")
        if isinstance(extensions, dict):
            extra.update(extensions)

        chub_meta = compact_dict(
            {
                "id": message.get("id"),
                "parent_id": message.get("parent_id"),
                "child_ids": message.get("child_ids"),
                "chat_id": message.get("chat_id"),
                "speaker_id": message.get("speaker_id"),
                "is_bot": message.get("is_bot"),
                "is_main": message.get("is_main"),
                "level": message.get("level"),
                "model_id": message.get("model_id"),
                "color": message.get("color"),
            }
        )
        if chub_meta:
            extra["chub"] = chub_meta

        rows.append(
            {
                "name": message_speaker_name(payload, message, character_name, user_name),
                "is_user": not is_bot,
                "is_system": False,
                "send_date": str(first_nonempty(message.get("created_at"), message.get("createdAt"))),
                "mes": text,
                "extra": extra,
                "swipe_id": 0,
                "swipes": [text],
            }
        )
    return rows


def write_jsonl_payload(path, payload):
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in payload_to_jsonl_rows(payload):
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def bot_png_path_for_chat_path(chat_path):
    base = re.sub(r"-chat-\d{3,}$", "", chat_path.stem)
    return chat_path.with_name(f"{base}.png")


def as_absolute_url(value):
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if value.startswith("//"):
        return f"https:{value}"
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return ""


def card_png_url_from_full_path(full_path):
    if not isinstance(full_path, str) or not full_path.strip():
        return ""
    encoded_path = "/".join(quote(part.strip(), safe="%") for part in full_path.strip("/").split("/") if part.strip())
    if not encoded_path:
        return ""
    return CHUB_CARD_PNG_URL.format(full_path=encoded_path)


def find_bot_png_url(payload):
    preferred_keys = (
        "png_url",
        "pngUrl",
        "card_png_url",
        "cardPngUrl",
        "character_card_url",
        "characterCardUrl",
        "card_url",
        "cardUrl",
    )
    fallback_keys = (
        "image",
        "image_url",
        "imageUrl",
        "avatar",
        "avatar_url",
        "avatarUrl",
    )

    for character in entity_map_from_payload(payload, "characters").values():
        for key in ("fullPath", "full_path", "publicPath", "public_path", "path"):
            url = card_png_url_from_full_path(character.get(key))
            if url:
                return url

        for key in preferred_keys:
            url = as_absolute_url(character.get(key))
            if url:
                return url

    for character in entity_map_from_payload(payload, "characters").values():
        for key in fallback_keys:
            url = as_absolute_url(character.get(key))
            if url:
                return url
    return ""


def download_bot_png_for_payload(payload, chat_path, request_context, request_headers=None):
    if request_context is None:
        raise RuntimeError("No browser request context available.")

    png_path = bot_png_path_for_chat_path(chat_path)
    if png_path.exists():
        log(f"Bot PNG already exists: {png_path.name}")
        return png_path

    png_url = find_bot_png_url(payload)
    if not png_url:
        raise RuntimeError("Could not find a bot PNG URL in the chat payload.")

    response = request_context.get(png_url, headers=request_headers or {}, timeout=60000)
    if response.status != 200:
        raise RuntimeError(f"Bot PNG request failed: HTTP {response.status}")

    body = response.body()
    if not body.startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError("Bot card response was not a PNG file.")

    png_path.write_bytes(body)
    log(f"Saved bot PNG: {png_path.name}")
    return png_path


def character_name_from_payload(payload):
    for entity in entity_map_from_payload(payload, "characters").values():
        name = entity_display_name(entity)
        cleaned = sanitize_filename(name, fallback="")
        if cleaned and not re.fullmatch(r"\d+", cleaned):
            return cleaned

    candidates = []

    def walk(value, depth=0):
        if depth > 4 or len(candidates) >= 20:
            return
        if isinstance(value, dict):
            for key in ("character_name", "characterName", "char_name", "charName", "name", "title"):
                item = value.get(key)
                if isinstance(item, str) and item.strip():
                    candidates.append(item.strip())
            for key in ("character", "char", "bot", "card", "project", "node", "meta", "config"):
                if key in value:
                    walk(value[key], depth + 1)
            if depth < 2:
                for item in value.values():
                    if isinstance(item, (dict, list)):
                        walk(item, depth + 1)
        elif isinstance(value, list):
            for item in value[:10]:
                walk(item, depth + 1)

    walk(payload)
    for candidate in candidates:
        cleaned = sanitize_filename(candidate, fallback="")
        if cleaned and not re.fullmatch(r"\d+", cleaned):
            return cleaned
    return f"chat-{chat_id_from_payload(payload)}"


def chat_id_from_payload(payload):
    if isinstance(payload, dict):
        for key in ("id", "chat_id", "chatId"):
            value = payload.get(key)
            if value not in (None, ""):
                return str(value)
    return "unknown"


def reusable_export_headers(raw_headers):
    skip = {
        "accept-encoding",
        "connection",
        "content-length",
        "host",
        "origin",
    }
    return {key: value for key, value in raw_headers.items() if key.lower() not in skip}


def prime_fast_export(page, chat_url, include_bot_png=False, target_path=None):
    captured = {}

    def on_request(request):
        if is_full_chat_export_request(request.url, chat_url):
            captured.clear()
            captured.update(reusable_export_headers(request.headers))

    page.on("request", on_request)
    try:
        export_one_chat(page, chat_url, include_bot_png=include_bot_png, target_path=target_path)
    finally:
        try:
            page.remove_listener("request", on_request)
        except Exception:
            pass

    if not captured:
        raise RuntimeError("Could not capture Chub's authorized full-chat request headers.")
    return captured


def fetch_chat_export_via_api(context, chat_url, headers, include_bot_png=False, target_path=None):
    response = context.request.get(full_chat_api_url(chat_url), headers=headers, timeout=60000)
    if response.status != 200:
        raise RuntimeError(f"Full chat API request failed: HTTP {response.status}")
    return save_payload_export(
        response.json(),
        chat_url,
        request_context=context.request,
        request_headers=headers,
        include_bot_png=include_bot_png,
        target_path=target_path,
    )


def load_links_from_file(path):
    source = Path(path)
    if not source.exists():
        return []
    links = []
    for line in source.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and is_probable_chat_url(line):
            links.append(line)
    return list(dict.fromkeys(links))


def run_list(args):
    with sync_playwright() as playwright:
        with browser_context(playwright) as context:
            page = first_page(context)
            collect_chat_links(page, max_scrolls=args.max_scrolls)


def run_export(args):
    done_urls = load_done_urls(getattr(args, "with_bot_png", False)) if args.resume else set()
    existing_paths = load_existing_jsonl_paths() if args.resume and getattr(args, "with_bot_png", False) else {}
    with sync_playwright() as playwright:
        with browser_context(playwright) as context:
            page = first_page(context)
            if args.from_file:
                links = load_links_from_file(args.from_file)
                log(f"Loaded {len(links)} chat link(s) from {args.from_file}")
            else:
                links = collect_chat_links(page, max_scrolls=args.max_scrolls)

            if args.limit:
                links = links[: args.limit]

            if not links:
                log("No chat links found. Make sure you are logged in and can see https://chub.ai/my_chats.")
                return

            exported = 0
            skipped = 0
            failed = 0

            for index, chat_url in enumerate(links, 1):
                if args.resume and chat_url in done_urls:
                    skipped += 1
                    log(f"[{index}/{len(links)}] Skipping already exported chat.")
                    continue

                log(f"[{index}/{len(links)}] Exporting chat.")
                try:
                    export_one_chat(
                        page,
                        chat_url,
                        include_bot_png=getattr(args, "with_bot_png", False),
                        target_path=existing_paths.get(chat_url),
                    )
                    exported += 1
                except Exception as exc:
                    failed += 1
                    log(f"FAILED {chat_url}: {exc}")
                    append_csv(
                        FAILED_FILE,
                        ["failed_at", "chat_url", "error"],
                        {
                            "failed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "chat_url": chat_url,
                            "error": str(exc),
                        },
                    )
                time.sleep(args.delay)

            log("------ Export Summary ------")
            log(f"Exported: {exported}")
            log(f"Skipped : {skipped}")
            log(f"Failed  : {failed}")


def run_export_all(args):
    done_urls = load_done_urls(getattr(args, "with_bot_png", False)) if args.resume else set()
    existing_paths = load_existing_jsonl_paths() if args.resume and getattr(args, "with_bot_png", False) else {}
    with sync_playwright() as playwright:
        with browser_context(playwright) as context:
            page = first_page(context)
            if args.from_file:
                links = load_links_from_file(args.from_file)
                log(f"Loaded {len(links)} chat link(s) from {args.from_file}")
            else:
                links = collect_chat_links(page, max_scrolls=args.max_scrolls)

            if args.limit:
                links = links[: args.limit]

            pending = [link for link in links if not (args.resume and link in done_urls)]
            skipped = len(links) - len(pending)
            if not pending:
                log("No pending chats to export.")
                return

            exported = 0
            failed = 0
            headers = None

            first_url = pending[0]
            log(f"[1/{len(pending)}] Priming export through Chub UI.")
            try:
                headers = prime_fast_export(
                    page,
                    first_url,
                    include_bot_png=getattr(args, "with_bot_png", False),
                    target_path=existing_paths.get(first_url),
                )
                exported += 1
            except Exception as exc:
                failed += 1
                log(f"FAILED {first_url}: {exc}")
                append_csv(
                    FAILED_FILE,
                    ["failed_at", "chat_url", "error"],
                    {
                        "failed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "chat_url": first_url,
                        "error": str(exc),
                    },
                )
                log("Export priming failed.")
                return

            for index, chat_url in enumerate(pending[1:], 2):
                log(f"[{index}/{len(pending)}] Exporting chat: {chat_url}")
                try:
                    fetch_chat_export_via_api(
                        context,
                        chat_url,
                        headers,
                        include_bot_png=getattr(args, "with_bot_png", False),
                        target_path=existing_paths.get(chat_url),
                    )
                    exported += 1
                except Exception as exc:
                    failed += 1
                    log(f"FAILED {chat_url}: {exc}")
                    append_csv(
                        FAILED_FILE,
                        ["failed_at", "chat_url", "error"],
                        {
                            "failed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "chat_url": chat_url,
                            "error": str(exc),
                        },
                    )
                time.sleep(args.delay)

            log("------ Export Summary ------")
            log(f"Exported: {exported}")
            log(f"Skipped : {skipped}")
            log(f"Failed  : {failed}")


def add_export_arguments(export):
    export.add_argument("--max-scrolls", type=int, default=DEFAULT_SCROLLS)
    export.add_argument("--limit", type=int, default=0, help="Only export the first N collected chats.")
    export.add_argument("--delay", type=float, default=FAST_API_DELAY_SECONDS, help="Delay between API exports.")
    export.add_argument("--from-file", default="", help="Export links from a saved chat_links.txt instead of rescanning.")
    export.add_argument("--resume", action=argparse.BooleanOptionalAction, default=True, help="Skip URLs already in export_manifest.csv.")
    export.add_argument("--with-bot-png", action="store_true", help="Also save the character card PNG for each exported chat.")


def build_parser():
    parser = argparse.ArgumentParser(description="Mass-export your own Chub chats.")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("login", help="Open Chub with a saved browser profile so you can log in.")

    export = sub.add_parser("export", help="Export all chats found on my_chats.")
    add_export_arguments(export)

    export_with_bots = sub.add_parser("export-with-bots", help="Export all chats and save bot card PNGs.")
    add_export_arguments(export_with_bots)
    export_with_bots.set_defaults(with_bot_png=True)

    return parser


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "login":
        ensure_dirs()
        run_login()
    elif args.command in ("export", "export-with-bots"):
        ensure_dirs()
        run_export_all(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
