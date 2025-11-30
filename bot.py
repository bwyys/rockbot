import discord
from discord.ext import commands
import random
from typing import Optional, List, Dict, Any
import aiohttp
import io
import json
import os
import string
import csv

TOKEN = os.getenv("DISCORD_TOKEN")
print("DEBUG: DISCORD_TOKEN from env:", repr(TOKEN))
print("DEBUG ENV KEYS:", [k for k in os.environ.keys() if "DISCORD" in k or "TOK" in k])


# 🔗 Google Sheets CSV URL (replace this with your real URL)
CSV_URL = "https://docs.google.com/spreadsheets/d/1oV3XHbkhez2SgCxNGZpqxNnGoB7GppmcdHKLX98b9K4/export?format=csv&gid=0"

STATS_FILE = "stats.json"

# -------- Fallback rocks (used only if Google Sheet load fails) --------
FALLBACK_ROCKS: List[Dict[str, Any]] = [
    {
        "id": 1,
        "name": "Bornite",
        "aliases": ["Peacock ore"],
        "images": [
            "https://upload.wikimedia.org/wikipedia/commons/9/95/Bornite-Quartz-135210.jpg"
        ],
        "hardness": "3",
        "luster": "Metallic",
        "streak": "Gray-black",
        "category": "Sulfide",
    },
    {
        "id": 2,
        "name": "Rose Quartz",
        "aliases": ["Rock crystal"],
        "images": [
            "https://upload.wikimedia.org/wikipedia/commons/d/db/Rose_quartz_-_01.jpg"
        ],
        "hardness": "7",
        "luster": "Vitreous",
        "streak": "White",
        "category": "Silicate (Quartz)",
    },
    {
        "id": 3,
        "name": "Obsidian",
        "aliases": ["Volcanic glass"],
        "images": [
            "https://upload.wikimedia.org/wikipedia/commons/f/fb/Obsidian.jpg",
            "https://upload.wikimedia.org/wikipedia/commons/a/ae/Obsidian_-_Igneous_Rock.jpg",
        ],
        "hardness": "5–5.5",
        "luster": "Glassy",
        "streak": "None (harder than streak plate)",
        "category": "Volcanic glass (igneous)",
    },
]

# This will hold the active dataset
ROCKS: List[Dict[str, Any]] = FALLBACK_ROCKS.copy()

# channel_id -> {"rock": {...}, "current_image": str}
ACTIVE_QUESTIONS: Dict[int, Dict[str, Any]] = {}

# -------- Stats handling --------
# stats structure: { user_id(str): {"total": int, "correct": int, "streak": int, "max_streak": int} }

def load_stats(path: str = STATS_FILE) -> Dict[str, Dict[str, int]]:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        return {}
    return data

def save_stats(stats: Dict[str, Dict[str, int]], path: str = STATS_FILE) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)

STATS: Dict[str, Dict[str, int]] = load_stats()

def update_stats(user_id: int, correct: bool) -> None:
    uid = str(user_id)
    if uid not in STATS:
        STATS[uid] = {"total": 0, "correct": 0, "streak": 0, "max_streak": 0}
    s = STATS[uid]
    s["total"] += 1
    if correct:
        s["correct"] += 1
        s["streak"] += 1
        if s["streak"] > s["max_streak"]:
            s["max_streak"] = s["streak"]
    else:
        s["streak"] = 0
    save_stats(STATS)

# -------- Utility Functions --------

def levenshtein(a: str, b: str) -> int:
    """Simple Levenshtein distance (edit distance)."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)

    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        curr = [i]
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            curr.append(
                min(
                    prev[j] + 1,        # deletion
                    curr[j - 1] + 1,    # insertion
                    prev[j - 1] + cost  # substitution
                )
            )
        prev = curr
    return prev[-1]


def normalize(text: str) -> str:
    """Lowercase and strip out non-alphanumerics for comparison."""
    text = text.lower()
    allowed = string.ascii_lowercase + string.digits
    return "".join(ch for ch in text if ch in allowed)


def is_correct_guess(raw_guess: str, rock: Dict[str, Any]) -> bool:
    """
    Stricter matching rules:
    - normalize
    - exact match vs full name/aliases
    - match any *word* in the name/aliases (e.g. 'quartz' -> 'Rose Quartz')
    - small typo tolerance, but only when lengths are similar
    """
    g = normalize(raw_guess)
    if not g or len(g) < 3:
        # super short guesses like 're', 'x', etc. are never accepted
        return False

    answers_raw = [rock["name"]] + rock.get("aliases", [])
    answers_norm = [normalize(a) for a in answers_raw]

    # --- 1) Exact full-name / alias match ---
    if g in answers_norm:
        return True

    # --- 2) Match any word in the name/aliases (for 'quartz' in 'Rose Quartz') ---
    word_norms = []
    for raw in answers_raw:
        for w in raw.replace("-", " ").split():
            wn = normalize(w)
            if wn and len(wn) >= 4:  # avoid tiny words like 'of', 're', etc.
                word_norms.append(wn)

    if g in word_norms:
        return True

    # --- 3) Tight typo tolerance on full names/aliases ---
    # Only consider answers of similar length; no substring cheating.
    for ans in answers_norm:
        if not ans:
            continue

        # lengths must be reasonably close (avoid 're' vs 'bornite')
        if abs(len(ans) - len(g)) > 2:
            continue

        dist = levenshtein(g, ans)

        # Allow:
        # - distance 1 always (single-character mistake)
        # - distance 2 only for longer words, and still < ~25% of length
        if dist == 0:
            return True
        if dist == 1 and len(ans) >= 4:
            return True
        if len(ans) >= 6 and dist == 2 and (dist / len(ans)) <= 0.25:
            return True

    return False

def choose_image(rock: Dict[str, Any], exclude: Optional[str] = None) -> str:
    """Pick a random image while avoiding the previous one."""
    images = rock["images"]
    if exclude and len(images) > 1:
        options = [img for img in images if img != exclude]
        if options:
            return random.choice(options)
    return random.choice(images)


async def send_image_file(ctx: commands.Context, url: str):
    """Download the image and upload it as a file."""
    print("Downloading image:", url)

    headers = {
        "User-Agent": "RockAndRollDiscordBot/1.3 (contact: youremail@example.com)"
    }

    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                await ctx.send(f"(Image download failed, status {resp.status}) {url}")
                return
            data = await resp.read()

    file = discord.File(io.BytesIO(data), filename="rock.jpg")
    await ctx.send(file=file)


# -------- Load rocks from Google Sheets CSV --------

async def fetch_rocks_from_google() -> List[Dict[str, Any]]:
    if not CSV_URL or "SPREADSHEET_ID" in CSV_URL:
        print("CSV_URL not set correctly; using fallback rocks.")
        return FALLBACK_ROCKS.copy()

    async with aiohttp.ClientSession() as session:
        async with session.get(CSV_URL) as resp:
            if resp.status != 200:
                print(f"Failed to fetch CSV (status {resp.status}), using fallback.")
                return FALLBACK_ROCKS.copy()
            text = await resp.text()

    reader = csv.DictReader(text.splitlines())
    rocks: List[Dict[str, Any]] = []

    for row in reader:
        name = (row.get("name") or "").strip()
        if not name:
            continue  # skip blank rows

        # ID (optional, but nice)
        rid_str = (row.get("id") or "").strip()
        try:
            rid = int(rid_str) if rid_str else len(rocks) + 1
        except ValueError:
            rid = len(rocks) + 1

        aliases_raw = row.get("aliases", "") or ""
        images_raw = row.get("images", "") or ""

        aliases = [a.strip() for a in aliases_raw.split("|") if a.strip()]
        images = [u.strip() for u in images_raw.split("|") if u.strip()]

        if not images:
            # don't break the bot if someone forgets images; just skip these
            print(f"Warning: rock '{name}' has no images; skipping.")
            continue

        rock = {
            "id": rid,
            "name": name,
            "aliases": aliases,
            "images": images,
            "hardness": (row.get("hardness") or "Unknown").strip(),
            "luster": (row.get("luster") or "Unknown").strip(),
            "streak": (row.get("streak") or "Unknown").strip(),
            "category": (row.get("category") or "Unknown").strip(),
        }
        rocks.append(rock)

    if not rocks:
        print("No valid rocks found in CSV; using fallback.")
        return FALLBACK_ROCKS.copy()

    return rocks


async def refresh_rocks_from_google() -> int:
    """Fetch rocks from Google Sheet and replace ROCKS."""
    global ROCKS
    try:
        new_rocks = await fetch_rocks_from_google()
        ROCKS = new_rocks
        print(f"Loaded {len(ROCKS)} rocks from Google Sheets.")
        return len(ROCKS)
    except Exception as e:
        print("Error loading rocks from Google Sheets:", e)
        print("Keeping existing ROCKS.")
        return len(ROCKS)


async def show_rock_view(ctx: commands.Context):
    """
    Used by r.r and r.p — one game per channel:
    - If no active rock in this channel → start one.
    - If there is one → show another image of the SAME rock.
    """
    if not ROCKS:
        await ctx.send(
            "No rocks are loaded. Ask the bot owner to configure the Google Sheets CSV URL."
        )
        return

    channel_id = ctx.channel.id
    had_active = channel_id in ACTIVE_QUESTIONS

    if not had_active:
        rock = random.choice(ROCKS)
        img = choose_image(rock)
        ACTIVE_QUESTIONS[channel_id] = {
            "rock": rock,
            "current_image": img,
        }
        header = "Here you go! New rock for this channel."
    else:
        state = ACTIVE_QUESTIONS[channel_id]
        rock = state["rock"]
        last_img = state["current_image"]
        img = choose_image(rock, exclude=last_img)
        state["current_image"] = img
        header = "Another view of the same rock."

    await ctx.send(f"**{header}**  (Use `r.help` for commands.)")
    await send_image_file(ctx, img)


# -------- BOT SETUP (CASE-INSENSITIVE PREFIX & COMMANDS) --------

intents = discord.Intents.default()
intents.message_content = True

def get_prefix(bot, message):
    # Accept both "r." and "R." as prefixes
    return ["r.", "R."]

bot = commands.Bot(
    command_prefix=get_prefix,
    intents=intents,
    help_command=None,
    case_insensitive=True,
)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    # Load rocks from Google Sheets at startup
    count = await refresh_rocks_from_google()
    print(f"Active rocks in dataset: {count}")
    print(f"Loaded stats for {len(STATS)} users from {STATS_FILE}")
    print("Commands: r.r / r.p / r.c <guess> / r.h / r.q / r.stats / r.lb / r.reload / r.help")


# -------- GAME COMMANDS --------

@bot.command(name="r")
async def cmd_r(ctx: commands.Context):
    """Start or repeat a rock in this channel."""
    await show_rock_view(ctx)


@bot.command(name="p")
async def cmd_p(ctx: commands.Context):
    """Alias of r.r: show another view of the channel's rock."""
    await show_rock_view(ctx)


@bot.command(name="c")
async def cmd_c(ctx: commands.Context, *, guess: Optional[str] = None):
    """
    Check answer and end round (CHANNEL-BASED).
    Anyone in the channel can answer the active rock.
    """
    channel_id = ctx.channel.id

    if channel_id not in ACTIVE_QUESTIONS:
        await ctx.send("No active rock in this channel. Use `r.r` to start one.")
        return

    if guess is None:
        await ctx.send("Usage: `r.c <guess>`")
        return

    state = ACTIVE_QUESTIONS[channel_id]
    rock = state["rock"]
    img = state["current_image"]

    correct = is_correct_guess(guess, rock)
    update_stats(ctx.author.id, correct)

    if correct:
        msg = (
            f"✅ Correct! <@{ctx.author.id}> got it.\n"
            f"It was **{rock['name']}**.\n"
            "Use `r.r` for a new rock in this channel."
        )
    else:
        msg = (
            f"❌ Incorrect, <@{ctx.author.id}>.\n"
            f"Your guess: `{guess}`\n"
            f"Correct answer: **{rock['name']}**.\n"
            "Use `r.r` for a new rock in this channel."
        )

    # End the round for this channel
    del ACTIVE_QUESTIONS[channel_id]

    await ctx.send(msg)
    await send_image_file(ctx, img)


@bot.command(name="h", aliases=["hint"])
async def cmd_h(ctx: commands.Context):
    """Show rock properties as a hint for this channel's rock."""
    channel_id = ctx.channel.id

    if channel_id not in ACTIVE_QUESTIONS:
        await ctx.send("No active rock in this channel. Use `r.r` first.")
        return

    state = ACTIVE_QUESTIONS[channel_id]
    rock = state["rock"]

    hardness = rock.get("hardness", "Unknown")
    luster = rock.get("luster", "Unknown")
    streak = rock.get("streak", "Unknown")
    category = rock.get("category", "Unknown")
    density = rock.get("density", "Unknown")   # NEW FIELD

    lines = [
        "Hint for this channel:",
        f"• Hardness: {hardness}",
        f"• Luster: {luster}",
        f"• Streak: {streak}",
        f"• Category: {category}",
        f"• Density: {density}",   # NEW
    ]

    await ctx.send("\n".join(lines))

@bot.command(name="q", aliases=["quit"])
async def cmd_q(ctx: commands.Context):
    """
    Quit current rock for this channel and reveal the answer.
    Anyone can call this.
    """
    channel_id = ctx.channel.id

    if channel_id not in ACTIVE_QUESTIONS:
        await ctx.send("No active rock to quit in this channel.")
        return

    state = ACTIVE_QUESTIONS[channel_id]
    rock = state["rock"]
    answer = rock["name"]

    # Show the answer before clearing
    await ctx.send(
        f"🛑 Round ended. The correct answer was **{answer}**.\n"
        "Use `r.r` to start a new rock for this channel."
    )

    # Clear the game
    del ACTIVE_QUESTIONS[channel_id]

# -------- STATS COMMANDS --------

@bot.command(name="stats")
async def cmd_stats(ctx: commands.Context):
    """Show your Rock & Roll stats."""
    uid = str(ctx.author.id)
    s = STATS.get(uid)
    if not s:
        await ctx.send("You don't have any stats yet. Play with `r.r` and `r.c`!")
        return

    total = s["total"]
    correct = s["correct"]
    acc = (correct / total * 100) if total > 0 else 0.0
    streak = s["streak"]
    max_streak = s["max_streak"]

    await ctx.send(
        f"📊 Stats for {ctx.author.mention}:\n"
        f"- Total guesses: **{total}**\n"
        f"- Correct guesses: **{correct}**\n"
        f"- Accuracy: **{acc:.1f}%**\n"
        f"- Current streak: **{streak}**\n"
        f"- Best streak: **{max_streak}**"
    )


@bot.command(name="lb", aliases=["leaderboard"])
async def cmd_leaderboard(ctx: commands.Context, mode: Optional[str] = None):
    """
    Show leaderboard.

    r.lb           -> top by correct answers
    r.lb acc       -> top by accuracy (with a minimum # of guesses)
    r.lb streak    -> top by best streak
    """
    if not STATS:
        await ctx.send("No stats yet. Play some rounds first!")
        return

    mode = (mode or "correct").lower()

    async def get_display_name(uid: str) -> str:
        uid_int = int(uid)
        user = bot.get_user(uid_int)
        if user:
            return user.name
        try:
            fetched = await bot.fetch_user(uid_int)
            return fetched.name
        except Exception:
            return f"User {uid}"

    items = list(STATS.items())

    if mode == "acc":
        MIN_ATTEMPTS = 10
        filtered = [
            (uid, s)
            for uid, s in items
            if s["total"] >= MIN_ATTEMPTS and s["total"] > 0
        ]
        if not filtered:
            await ctx.send(
                f"Not enough data yet (need ≥{MIN_ATTEMPTS} guesses per player)."
            )
            return

        def sort_key(item):
            uid, s = item
            total = s["total"]
            correct = s["correct"]
            acc = correct / total
            return (-acc, -total)

        sorted_items = sorted(filtered, key=sort_key)[:10]
        title = "🏆 **Leaderboard – Accuracy** (min 10 guesses)"

    elif mode == "streak":
        def sort_key(item):
            uid, s = item
            return (-s.get("max_streak", 0), -s["correct"])

        sorted_items = sorted(items, key=sort_key)[:10]
        title = "🔥 **Leaderboard – Best Streaks**"

    else:
        def sort_key(item):
            uid, s = item
            total = s["total"]
            correct = s["correct"]
            acc = correct / total if total > 0 else 0
            return (-correct, -acc, -total)

        sorted_items = sorted(items, key=sort_key)[:10]
        title = "🏆 **Leaderboard – Most Correct Answers**"

    lines = []
    for rank, (uid, s) in enumerate(sorted_items, start=1):
        name = await get_display_name(uid)
        total = s["total"]
        correct = s["correct"]
        acc = (correct / total * 100) if total > 0 else 0.0
        streak = s.get("max_streak", 0)

        if mode == "acc":
            line = (
                f"{rank}. **{name}** — {acc:.1f}% acc "
                f"({correct}/{total}, best streak {streak})"
            )
        elif mode == "streak":
            line = (
                f"{rank}. **{name}** — best streak {streak}, "
                f"{correct} correct out of {total} ({acc:.1f}% acc)"
            )
        else:
            line = (
                f"{rank}. **{name}** — {correct} correct / {total} total "
                f"({acc:.1f}% acc, best streak {streak})"
            )

        lines.append(line)

    msg = title + "\n" + "\n".join(lines)
    await ctx.send(msg)

# -------- RELOAD ROCKS FROM SHEET --------

@bot.command(name="reload")
@commands.is_owner()
async def cmd_reload(ctx: commands.Context):
    """Owner-only: reload rocks from Google Sheets."""
    count = await refresh_rocks_from_google()
    await ctx.send(f"Reloaded rocks from Google Sheets. Now have **{count}** entries.")

# -------- HELP --------

@bot.command(name="help")
async def cmd_help(ctx: commands.Context):
    """Simple one-line help."""
    await ctx.send(
        "**Rock & Roll Commands (per channel):**  "
        "`r.r`/`r.p` show or re-show the channel's rock · "
        "`r.c <guess>` check and end round · "
        "`r.h`/`r.hint` hint (properties) · "
        "`r.q` quit current rock · "
        "`r.stats` your stats · "
        "`r.lb` leaderboard (`r.lb acc`, `r.lb streak`) · "
        "`r.reload` reload rocks (bot owner only)"
    )

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN environment variable is not set!")


bot.run(TOKEN)
