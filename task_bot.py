#!/usr/bin/env python3
"""
Telegram → Trello: create cards from /task messages.

Runs on GitHub Actions every 10 minutes. Polls Telegram for new /task messages,
creates Trello cards on the specified board, optionally assigns members, and
sends a confirmation reply.

Message format:
    /task <board> [@person ...] Card title text

Examples:
    /task aso @нина Обновить мету для Succubus
    /task маркетинг @кирилл @сергей Запустить кампанию
    /task ai Задача без ответственного

Board alias is REQUIRED. Member tags (@name) are optional; multiple allowed.
Card lands in the first (leftmost) list of the board.

Required env vars (GitHub Actions secrets): TRELLO_KEY, TRELLO_TOKEN, TELEGRAM_TOKEN
"""

import os, sys, json, re, urllib.parse, urllib.request

TRELLO_KEY = os.environ["TRELLO_KEY"]
TRELLO_TOKEN = os.environ["TRELLO_TOKEN"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]

# ── Board aliases ─────────────────────────────────────────────────────────
BOARDS = {
    "ai":         "6a02f6425b2bab71667a0ec0",
    "конвейер":   "6a02f6425b2bab71667a0ec0",
    "aso":        "659fb4850fca7167e72f22f6",
    "nina":       "659fb4850fca7167e72f22f6",
    "маркетинг":  "659fab6e3fb12090fad1507f",
    "marketing":  "659fab6e3fb12090fad1507f",
    "nga":        "659fab6e3fb12090fad1507f",
    "leadtech":   "691d7639195df16bba00b496",
    "newgen":     "668d4d34afa29c08e30dbc48",
    "newgenapps": "668d4d34afa29c08e30dbc48",
    "app":        "672b4de35020a5be7652bd06",
    "креативы":   "674d6fc88c3285d37d80e163",
    "баги":       "639c4028c24de80172765d82",
    "bugs":       "639c4028c24de80172765d82",
}
BOARD_NAMES = {
    "6a02f6425b2bab71667a0ec0": "Ai Конвейер",
    "659fb4850fca7167e72f22f6": "ASO Nina",
    "659fab6e3fb12090fad1507f": "Маркетинг NGA",
    "691d7639195df16bba00b496": "LeadTech",
    "668d4d34afa29c08e30dbc48": "NewGenApps",
    "672b4de35020a5be7652bd06": "App",
    "674d6fc88c3285d37d80e163": "Креативы",
    "639c4028c24de80172765d82": "Фикс багов",
}

# ── Person aliases (lowercase) → substrings matched against Trello fullName
#    The script fetches board members and finds those whose fullName contains
#    the substring.  This way you don't need to hardcode Trello member ids.
PEOPLE = {
    "нина":    "Нина",
    "nina":    "Нина",
    "виталий": "Виталий",
    "vitaliy": "Виталий",
    "сергей":  "Сергей",
    "sergey":  "Сергей",
    "кирилл":  "Кирилл",
    "kirill":  "Кирилл",
    "антон":   "Антон",
    "anton":   "Антон",
}

_list_cache = {}
_members_cache = {}   # board_id → [{id, fullName}, ...]
_green_label_cache = {}  # board_id → green label id

# ── HTTP ──────────────────────────────────────────────────────────────────

def telegram_api(method, params=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
    data = urllib.parse.urlencode(params or {}).encode("utf-8")
    req = urllib.request.Request(url, data=data) if params else urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))

def trello_get(path, params=None):
    p = dict(params or {}); p["key"] = TRELLO_KEY; p["token"] = TRELLO_TOKEN
    url = "https://api.trello.com/1/" + path + "?" + urllib.parse.urlencode(p)
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))

def trello_post(path, params):
    p = dict(params); p["key"] = TRELLO_KEY; p["token"] = TRELLO_TOKEN
    data = urllib.parse.urlencode(p).encode("utf-8")
    req = urllib.request.Request("https://api.trello.com/1/" + path, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))

# ── Trello helpers ────────────────────────────────────────────────────────

def get_first_list_id(board_id):
    if board_id in _list_cache:
        return _list_cache[board_id]
    lists = trello_get(f"boards/{board_id}/lists", {"filter": "open", "fields": "name"})
    if not lists:
        raise RuntimeError(f"Board {board_id} has no open lists")
    _list_cache[board_id] = lists[0]["id"]
    print(f"DIAG: board first list = {lists[0]['name']}")
    return lists[0]["id"]


def get_board_members(board_id):
    if board_id in _members_cache:
        return _members_cache[board_id]
    members = trello_get(f"boards/{board_id}/members", {"fields": "fullName,username"})
    _members_cache[board_id] = members
    print(f"DIAG: board has {len(members)} members: "
          + ", ".join(m.get("fullName", m.get("username", "?")) for m in members))
    return members


def resolve_member_ids(board_id, person_tags):
    """Given a list of lowercase person tags (e.g. ['нина', 'антон']),
    find matching Trello member ids on the board.
    Returns (found_ids, found_names, not_found_tags)."""
    members = get_board_members(board_id)
    found_ids = []
    found_names = []
    not_found = []
    for tag in person_tags:
        search = PEOPLE.get(tag)
        if not search:
            not_found.append(tag)
            continue
        matched = False
        for m in members:
            if search.lower() in (m.get("fullName") or "").lower():
                if m["id"] not in found_ids:
                    found_ids.append(m["id"])
                    found_names.append(m.get("fullName") or m.get("username"))
                matched = True
                break
        if not matched:
            not_found.append(tag)
    return found_ids, found_names, not_found


def get_green_label_id(board_id):
    """Find the green label on the board. Returns its id, or None."""
    if board_id in _green_label_cache:
        return _green_label_cache[board_id]
    labels = trello_get(f"boards/{board_id}/labels", {"fields": "color,name", "limit": "1000"})
    for lb in labels:
        color = (lb.get("color") or "").lower()
        if color.startswith("green"):
            _green_label_cache[board_id] = lb["id"]
            print(f"DIAG: green label = {lb['id']} ({color}/{lb.get('name','')})")
            return lb["id"]
    # No green label exists yet — create one.
    new_label = trello_post(f"boards/{board_id}/labels", {"name": "", "color": "green"})
    _green_label_cache[board_id] = new_label["id"]
    print(f"DIAG: created green label = {new_label['id']}")
    return new_label["id"]


def create_card(board_id, card_name, member_ids=None):
    list_id = get_first_list_id(board_id)
    green_id = get_green_label_id(board_id)
    params = {
        "name": card_name,
        "idList": list_id,
        "pos": "top",
        "idLabels": green_id,
    }
    if member_ids:
        params["idMembers"] = ",".join(member_ids)
    card = trello_post("cards", params)
    return card.get("shortUrl", card.get("url", ""))


# ── Parse /task message ──────────────────────────────────────────────────

def parse_task_message(text):
    """Parse: /task <board> [@person ...] card title
    Returns dict with keys: board_id, board_name, title, person_tags
    Or special {"error": "..."} if something is wrong.
    Or None if not a /task message."""
    if not text:
        return None
    text = text.strip()
    if not text.lower().startswith("/task"):
        return None

    body = text[5:].strip()
    if not body:
        return None

    words = body.split()
    if not words:
        return None

    # First word must be a board alias.
    first = words[0].lower()
    if first not in BOARDS:
        return {"error": "no_board", "text": body}

    board_id = BOARDS[first]
    board_name = BOARD_NAMES.get(board_id, board_id)

    # Remaining words: extract @person tags and card title.
    rest = words[1:]
    person_tags = []
    title_words = []
    for w in rest:
        if w.startswith("@") and len(w) > 1:
            person_tags.append(w[1:].lower())
        else:
            title_words.append(w)

    title = " ".join(title_words).strip()
    if not title:
        return {"error": "no_title"}

    return {
        "board_id": board_id,
        "board_name": board_name,
        "title": title,
        "person_tags": person_tags,
    }


# ── Telegram ──────────────────────────────────────────────────────────────

def send_reply(chat_id, text):
    telegram_api("sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML"})


def send_help(chat_id):
    boards = sorted(set(BOARDS.keys()))
    people = sorted(set(PEOPLE.keys()))
    send_reply(chat_id,
        "<b>Как добавить карточку в Trello:</b>\n\n"
        "<code>/task [доска] [@ответственный] Текст задачи</code>\n\n"
        "<b>Примеры:</b>\n"
        "<code>/task aso @нина Обновить мету</code>\n"
        "<code>/task маркетинг @кирилл @сергей Запустить кампанию</code>\n"
        "<code>/task ai Задача без ответственного</code>\n\n"
        f"<b>Доски:</b> {', '.join(boards)}\n"
        f"<b>Люди:</b> @{', @'.join(people)}\n\n"
        "⚠️ Доска обязательна. Ответственные — по желанию.\n"
        "Карточка создаётся в первом столбце доски."
    )


def process_updates():
    result = telegram_api("getUpdates")
    if not result.get("ok"):
        raise RuntimeError(f"getUpdates failed: {result}")

    updates = result.get("result", [])
    if not updates:
        print("No new messages.")
        return

    max_id = 0
    created = 0

    for u in updates:
        max_id = max(max_id, u["update_id"])
        msg = u.get("message") or u.get("edited_message") or {}
        text = msg.get("text", "")
        chat_id = msg.get("chat", {}).get("id")
        if not chat_id:
            continue

        if text.strip().lower() in ("/start", "/help"):
            send_help(chat_id)
            continue

        parsed = parse_task_message(text)
        if parsed is None:
            continue

        # Error cases
        if isinstance(parsed, dict) and "error" in parsed:
            if parsed["error"] == "no_board":
                aliases = sorted(set(BOARDS.keys()))
                send_reply(chat_id,
                    "⚠️ <b>Укажи доску!</b>\n\n"
                    f"<b>Доступные:</b> {', '.join(aliases)}\n\n"
                    "<code>/task [доска] [@ответственный] текст</code>")
            elif parsed["error"] == "no_title":
                send_reply(chat_id, "⚠️ <b>Напиши текст задачи после доски.</b>")
            continue

        board_id = parsed["board_id"]
        board_name = parsed["board_name"]
        title = parsed["title"]
        person_tags = parsed["person_tags"]

        try:
            member_ids, member_names, not_found = [], [], []
            if person_tags:
                member_ids, member_names, not_found = resolve_member_ids(board_id, person_tags)

            card_url = create_card(board_id, title, member_ids or None)

            lines = [
                f"✅ <b>Карточка создана</b>",
                f"📋 {board_name}",
                f"📝 {title}",
            ]
            if member_names:
                lines.append(f"👤 {', '.join(member_names)}")
            if not_found:
                lines.append(f"⚠️ Не найдены на доске: @{', @'.join(not_found)}")
            lines.append(f'🔗 <a href="{card_url}">Открыть в Trello</a>')

            send_reply(chat_id, "\n".join(lines))
            created += 1
            assignee_str = f" → {', '.join(member_names)}" if member_names else ""
            print(f"Created: [{board_name}] {title}{assignee_str}")

        except Exception as e:
            send_reply(chat_id, f"❌ Ошибка: {e}")
            print(f"FAILED: {e}", file=sys.stderr)

    if max_id:
        telegram_api("getUpdates", {"offset": max_id + 1})

    print(f"Done: {len(updates)} update(s), {created} card(s) created.")


def main():
    try:
        process_updates()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
