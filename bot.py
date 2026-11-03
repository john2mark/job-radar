"""
Job Radar - Discord bot (complete version)
Needs job_finder.py in the same folder and a .env file with DISCORD_TOKEN.

Commands: /setup /help /skills /jobs /links /digest /check /note
          /apply /applications /status /prefs /report
"""
import asyncio
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional
from urllib.parse import quote

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

import job_finder as jf

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")  # optional, makes commands appear instantly
_base = os.path.join(os.getenv("LOCALAPPDATA") or os.path.expanduser("~"), "JobRadar")
os.makedirs(_base, exist_ok=True)
DB = os.path.join(_base, "jobradar.db")
NIGERIA = timezone(timedelta(hours=1))  # Nigeria has no daylight saving time
COLOR = 0x0F6B63
WARN_COLOR = 0xA4410D

CHANNELS = {
    "start-here": "How to use Job Radar",
    "my-skills": "Set up your skills with /skills",
    "job-search": "Run /jobs to find remote jobs",
    "new-matches": "Daily digest of new matches",
    "posting-check": "Check a job posting with /check",
    "applications": "Track where you applied",
}

HELP = (
    "**Start:** `/skills add python, figma` (your skills), then `/jobs` in #job-search.\n"
    "**Find:** `/jobs` ranks remote jobs for you (add `role:researcher`). `/links` gives LinkedIn and Reddit "
    "searches. `/digest on` posts new matches in #new-matches each morning.\n"
    "**Check:** `/check` scans a posting for fit, pay and scam signs. `/report add` warns others about a bad employer.\n"
    "**Track:** click a **Save** button under a job, or use `/apply`. `/applications` lists them, `/status` updates "
    "them, and you get a follow-up reminder a week after you apply.\n"
    "**Write:** `/note` drafts an application note.\n"
    "**Settings:** `/prefs show` for private results, location, excluded words and more."
)
GUIDE = "**Welcome to Job Radar**\n\n" + HELP


# ------------------------------------------------------------------ database
def run(sql, args=(), fetch=False):
    con = sqlite3.connect(DB)
    try:
        cur = con.execute(sql, args)
        rows = cur.fetchall() if fetch else None
        con.commit()
        return rows
    finally:
        con.close()


def ensure_column(table, column, decl):
    if column not in [r[1] for r in run(f"PRAGMA table_info({table})", fetch=True)]:
        run(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


run("CREATE TABLE IF NOT EXISTS skills (user_id INTEGER, skill TEXT, PRIMARY KEY (user_id, skill))")
run("CREATE TABLE IF NOT EXISTS channels (guild_id INTEGER, name TEXT, channel_id INTEGER, PRIMARY KEY (guild_id, name))")
run("CREATE TABLE IF NOT EXISTS digest (user_id INTEGER PRIMARY KEY, guild_id INTEGER, hour INTEGER, last_sent TEXT)")
run("CREATE TABLE IF NOT EXISTS seen (user_id INTEGER, url TEXT, PRIMARY KEY (user_id, url))")
run("""CREATE TABLE IF NOT EXISTS applications (
    user_id INTEGER, num INTEGER, company TEXT, role TEXT, url TEXT, status TEXT, created TEXT, updated TEXT,
    guild_id INTEGER, remind TEXT, reminded INTEGER DEFAULT 0, PRIMARY KEY (user_id, num))""")
for _col, _decl in (("guild_id", "INTEGER"), ("remind", "TEXT"), ("reminded", "INTEGER DEFAULT 0")):
    ensure_column("applications", _col, _decl)
run("""CREATE TABLE IF NOT EXISTS prefs (user_id INTEGER PRIMARY KEY, entry INTEGER DEFAULT 1,
    location TEXT DEFAULT 'hide restricted', private INTEGER DEFAULT 0, pay_only INTEGER DEFAULT 0,
    exclude TEXT DEFAULT '', intro TEXT DEFAULT '')""")
run("""CREATE TABLE IF NOT EXISTS reports (company_key TEXT, company TEXT, user_id INTEGER, reason TEXT,
    created TEXT, PRIMARY KEY (company_key, user_id))""")


def get_skills(user_id):
    return [r[0] for r in run("SELECT skill FROM skills WHERE user_id=? ORDER BY rowid", (user_id,), fetch=True)]


def get_prefs(user_id):
    rows = run("SELECT entry, location, private, pay_only, exclude, intro FROM prefs WHERE user_id=?", (user_id,), fetch=True)
    e, loc, pri, po, ex, intro = rows[0] if rows else (1, "hide restricted", 0, 0, "", "")
    return {"entry": bool(e), "location": loc, "private": bool(pri), "pay_only": bool(po),
            "exclude": [w for w in (ex or "").split(",") if w], "intro": intro or ""}


def set_pref(user_id, column, value):
    assert column in ("entry", "location", "private", "pay_only", "exclude", "intro")
    run("INSERT OR IGNORE INTO prefs (user_id) VALUES (?)", (user_id,))
    run(f"UPDATE prefs SET {column}=? WHERE user_id=?", (value, user_id))


def today_ng():
    return datetime.now(NIGERIA).strftime("%d %b %Y")


def iso_in(days):
    return (datetime.now(NIGERIA) + timedelta(days=days)).strftime("%Y-%m-%d")


def norm_company(name):
    key = re.sub(r"[^a-z0-9 ]", "", name.lower())
    key = re.sub(r"\b(inc|llc|ltd|limited|corp|co|company|gmbh)\b", "", key)
    return re.sub(r"\s+", " ", key).strip()


def report_counts():
    return {k: n for k, n in run("SELECT company_key, COUNT(*) FROM reports GROUP BY company_key", fetch=True)}


def add_application(user_id, guild_id, company, role, url, status):
    """Returns (number, is_new). Skips duplicates by link."""
    if url:
        found = run("SELECT num FROM applications WHERE user_id=? AND url=?", (user_id, url), fetch=True)
        if found:
            return found[0][0], False
    num = run("SELECT COALESCE(MAX(num), 0) FROM applications WHERE user_id=?", (user_id,), fetch=True)[0][0] + 1
    now = today_ng()
    run("""INSERT INTO applications (user_id, num, company, role, url, status, created, updated, guild_id, remind, reminded)
           VALUES (?,?,?,?,?,?,?,?,?,?,0)""",
        (user_id, num, company.strip()[:80], role.strip()[:100], url[:500], status, now, now, guild_id,
         iso_in(7) if status == "Applied" else None))
    return num, True


# ----------------------------------------------------------------------- bot
class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()  # can take up to an hour to show up
        digest_loop.start()
        reminder_loop.start()


bot = Bot()
tree = bot.tree


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}. Ready.")


def clean(text, limit):
    text = discord.utils.escape_markdown(str(text)).replace("[", "(").replace("]", ")")
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --------------------------------------------------------------- /setup /help
@tree.command(name="setup", description="Create the Job Radar channels and post the guide")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def setup(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    created = []
    for name, topic in CHANNELS.items():
        channel = discord.utils.get(guild.text_channels, name=name)
        if channel is None:
            try:
                channel = await guild.create_text_channel(name, topic=topic)
            except discord.Forbidden:
                await interaction.followup.send("I need the **Manage Channels** permission. Re-invite me with it, then run /setup again.")
                return
            created.append(name)
            if name == "start-here":
                try:
                    msg = await channel.send(GUIDE)
                    await msg.pin()
                except discord.Forbidden:
                    pass
        run("INSERT OR REPLACE INTO channels VALUES (?,?,?)", (guild.id, name, channel.id))
    text = f"Created: {', '.join('#' + c for c in created)}." if created else "All channels already exist."
    await interaction.followup.send(text + " Next, run `/skills add` to set up your skills.")


@tree.command(name="help", description="What Job Radar can do")
async def help_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(HELP, ephemeral=True)


# -------------------------------------------------------------------- /skills
skills_group = app_commands.Group(name="skills", description="Manage your skills")


@skills_group.command(name="add", description="Add skills, separated by commas")
@app_commands.describe(skills="For example: python, figma, customer support")
async def skills_add(interaction: discord.Interaction, skills: str):
    await interaction.response.defer(ephemeral=True)
    for s in [s.strip().lower() for s in skills.split(",") if s.strip()][:30]:
        run("INSERT OR IGNORE INTO skills VALUES (?,?)", (interaction.user.id, s[:50]))
    await interaction.followup.send(f"Saved. Your skills: {', '.join(get_skills(interaction.user.id)) or 'none'}")


@skills_group.command(name="list", description="Show your skills")
async def skills_list(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    mine = get_skills(interaction.user.id)
    await interaction.followup.send(", ".join(mine) if mine else "No skills yet. Add some with `/skills add`.")


@skills_group.command(name="remove", description="Remove one skill")
@app_commands.describe(skill="The skill to remove")
async def skills_remove(interaction: discord.Interaction, skill: str):
    await interaction.response.defer(ephemeral=True)
    run("DELETE FROM skills WHERE user_id=? AND skill=?", (interaction.user.id, skill.strip().lower()))
    await interaction.followup.send(f"Removed. Your skills: {', '.join(get_skills(interaction.user.id)) or 'none'}")


@skills_group.command(name="clear", description="Remove all your skills")
async def skills_clear(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    run("DELETE FROM skills WHERE user_id=?", (interaction.user.id,))
    await interaction.followup.send("All skills removed.")


tree.add_command(skills_group)


# ---------------------------------------------------------------------- /jobs
SENIOR_TITLE = re.compile(r"\b(senior|sr\.?|lead|principal|staff|head of|director|vp|chief|architect|manager)\b", re.I)
MANY_YEARS = re.compile(r"(?<![\d\-–])([3-9]|1\d)\s*\+?\s*(?:or more\s*)?years?\b", re.I)
ENTRY_WORDS = re.compile(r"entry[- ]level|junior|graduate|intern(ship)?|trainee|no experience|beginner|associate|assistant", re.I)
LOC_TAG = {"yes": " · worldwide ✓", "maybe": " · location not stated, check",
           "no": " · restricted, may not accept Nigeria"}


def search_jobs(skills, role, entry_level=True, location="hide restricted", exclude=(), pay_only=False):
    terms = ([role] if role else []) + skills
    api_terms = (["entry level", "junior", "assistant"] if entry_level else []) + terms
    jobs, counts = jf.fetch_all(skills, terms, api_terms, entry_level)
    seen, ranked = set(), []
    for job in jobs:
        if job["url"] in seen:
            continue
        seen.add(job["url"])
        if role and not any(jf.has_skill(job["title"], w) for w in role.split()):
            continue
        if entry_level and (SENIOR_TITLE.search(job["title"]) or MANY_YEARS.search(job["text"])):
            continue
        if exclude and any(jf.has_skill(job["title"] + " " + job["text"][:1500], w) for w in exclude):
            continue
        if pay_only and not job["pay"]:
            continue
        status = jf.location_status(job)
        if (location == "worldwide only" and status != "yes") or (location == "hide restricted" and status == "no"):
            continue
        hits, flagged = jf.score(job["text"], skills)
        if hits and not flagged:
            job["loc"] = status
            job["entry"] = bool(ENTRY_WORDS.search(job["title"] + " " + job["text"][:600]))
            ranked.append((len(hits), hits, job))
    ranked.sort(key=lambda t: t[0] + (1 if t[2]["entry"] else 0) + (2 if t[2]["loc"] == "yes" else 0), reverse=True)
    return ranked, counts


class SaveView(discord.ui.View):
    """One Save button per job. Whoever clicks it saves the job to their own tracker."""

    def __init__(self, items):
        super().__init__(timeout=86400)
        for i, item in enumerate(items[:8], 1):
            button = discord.ui.Button(label=f"Save {i}", style=discord.ButtonStyle.secondary)
            button.callback = self._make_callback(item)
            self.add_item(button)

    @staticmethod
    def _make_callback(item):
        title, company, url = item

        async def callback(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            num, is_new = add_application(interaction.user.id, interaction.guild_id, company, title, url, "Saved")
            await interaction.followup.send(
                f"Saved as **#{num}** in your list." if is_new else f"Already in your list as **#{num}**.")
        return callback


def jobs_embed(items, skills, title, footer):
    flagged = report_counts()
    lines = []
    for i, (n, hits, job) in enumerate(items[:8], 1):
        extra = ((f" · {clean(job['pay'], 40)}" if job["pay"] else "")
                 + (" · entry-level friendly" if job.get("entry") else "")
                 + LOC_TAG[job.get("loc", "maybe")])
        reports = flagged.get(norm_company(job["company"]), 0)
        if reports:
            extra += f" · ⚠ reported by {reports} member{'s' if reports > 1 else ''}"
        lines.append(f"**{i}.** [{clean(job['title'], 80)}]({job['url']}) at {clean(job['company'], 40)}\n"
                     f"Matches {n} of {len(skills)}: {', '.join(hits)}{extra} · {job['source']}")
    embed = discord.Embed(title=title, description="\n\n".join(lines)[:4000], color=COLOR)
    embed.set_footer(text=footer[:2000])
    return embed


def save_view_for(items):
    return SaveView([(j["title"], j["company"], j["url"]) for _, _, j in items[:8]])


@tree.command(name="jobs", description="Find remote jobs that match your skills")
@app_commands.describe(role="Optional job title, e.g. researcher",
                       entry_level="Hide senior roles and jobs asking 3+ years",
                       location="worldwide only = strict; hide restricted = hides US/EU-only jobs",
                       private="Show results only to you")
async def jobs(interaction: discord.Interaction, role: str = "", entry_level: Optional[bool] = None,
               location: Optional[Literal["hide restricted", "worldwide only", "show all"]] = None,
               private: Optional[bool] = None):
    prefs = get_prefs(interaction.user.id)
    entry = prefs["entry"] if entry_level is None else entry_level
    loc = prefs["location"] if location is None else location
    hide = prefs["private"] if private is None else private
    await interaction.response.defer(ephemeral=hide)
    skills = get_skills(interaction.user.id)
    if not skills:
        await interaction.followup.send("Add your skills first with `/skills add python, figma`.", ephemeral=True)
        return
    role = role.strip().lower()
    ranked, counts = await asyncio.to_thread(search_jobs, skills, role, entry, loc, prefs["exclude"], prefs["pay_only"])
    searched = "Searched: " + ", ".join(f"{k} {v}" for k, v in counts.items())
    if not ranked:
        await interaction.followup.send("No matches right now. Try broader skills, a different role, or loosen "
                                        "your `/prefs`.\n" + searched)
        return
    title = "Remote jobs for your skills" + (f" ({role})" if role else "")
    embed = jobs_embed(ranked, skills, title, searched + "\nAlways confirm pay and the company before sharing personal details.")
    await interaction.followup.send(embed=embed, view=save_view_for(ranked))


# --------------------------------------------------------------------- /links
@tree.command(name="links", description="Entry-level remote search links for LinkedIn and Reddit")
@app_commands.describe(role="Optional job title, e.g. research assistant")
async def links(interaction: discord.Interaction, role: str = ""):
    await interaction.response.defer(ephemeral=True)
    skills = get_skills(interaction.user.id)
    role = role.strip()
    terms = ([role] if role else []) + skills[:5]
    if not terms:
        await interaction.followup.send("Add your skills first with `/skills add`, or use `/links role:research assistant`.")
        return
    li = [f"[LinkedIn: {clean(t, 40)}](https://www.linkedin.com/jobs/search/?keywords={quote(t, safe='')}"
          f"&f_WT=2&f_E=1%2C2&f_TPR=r604800)" for t in terms]
    rq = quote("hiring " + terms[0], safe="")
    rd = [f"[r/{sub}: hiring {clean(terms[0], 30)}](https://www.reddit.com/r/{sub}/search/?q={rq}&restrict_sr=1&sort=new&t=week)"
          for sub in ("forhire", "remotejs", "jobbit", "hiring")]
    embed = discord.Embed(
        title="Entry-level remote searches",
        description="**LinkedIn** (remote, internship and entry level, past week)\n" + "\n".join(li)
                    + "\n\n**Reddit** (newest hiring posts)\n" + "\n".join(rd),
        color=COLOR)
    embed.set_footer(text="Sign in to LinkedIn first. Confirm pay and the company before applying.")
    await interaction.followup.send(embed=embed)


# --------------------------------------------------------------------- digest
def new_matches_for(user_id):
    skills = get_skills(user_id)
    if not skills:
        return None, [], {}
    p = get_prefs(user_id)
    ranked, counts = search_jobs(skills, "", p["entry"], p["location"], p["exclude"], p["pay_only"])
    seen = {r[0] for r in run("SELECT url FROM seen WHERE user_id=?", (user_id,), fetch=True)}
    return skills, [item for item in ranked if item[2]["url"] not in seen][:8], counts


async def send_digest(user_id, guild_id):
    """Post this user's new matches in #new-matches. Returns True if something was posted."""
    skills, fresh, counts = await asyncio.to_thread(new_matches_for, user_id)
    if skills is None or not fresh:
        return False
    row = run("SELECT channel_id FROM channels WHERE guild_id=? AND name='new-matches'", (guild_id,), fetch=True)
    if not row:
        return False
    channel = bot.get_channel(row[0][0]) or await bot.fetch_channel(row[0][0])
    searched = "Searched: " + ", ".join(f"{k} {v}" for k, v in counts.items())
    embed = jobs_embed(fresh, skills, "New remote jobs for you", searched + "\nOnly jobs you haven't been shown before.")
    await channel.send(content=f"<@{user_id}> your new matches", embed=embed, view=save_view_for(fresh))
    for _, _, job in fresh:
        run("INSERT OR IGNORE INTO seen VALUES (?,?)", (user_id, job["url"]))
    return True


@tasks.loop(minutes=5)
async def digest_loop():
    now = datetime.now(NIGERIA)
    today = now.strftime("%Y-%m-%d")
    rows = run("SELECT user_id, guild_id, hour FROM digest WHERE last_sent IS NULL OR last_sent != ?", (today,), fetch=True)
    for user_id, guild_id, hour in rows:
        if now.hour < hour:
            continue  # not time yet (if the laptop was off, it catches up later the same day)
        try:
            await send_digest(user_id, guild_id)
        except Exception as exc:
            print(f"[digest] failed for {user_id}: {exc}")
            continue
        run("UPDATE digest SET last_sent=? WHERE user_id=?", (today, user_id))


@digest_loop.before_loop
async def before_digest():
    await bot.wait_until_ready()


digest_group = app_commands.Group(name="digest", description="Daily job digest in #new-matches", guild_only=True)


@digest_group.command(name="on", description="Post your new matches in #new-matches every day")
@app_commands.describe(hour="Hour of the day in Nigeria time, 0-23 (default 8 = 8 AM)")
async def digest_on(interaction: discord.Interaction, hour: app_commands.Range[int, 0, 23] = 8):
    await interaction.response.defer(ephemeral=True)
    if not get_skills(interaction.user.id):
        await interaction.followup.send("Add your skills first with `/skills add`.")
        return
    if not run("SELECT 1 FROM channels WHERE guild_id=? AND name='new-matches'", (interaction.guild.id,), fetch=True):
        await interaction.followup.send("The #new-matches channel isn't set up yet. Ask an admin to run `/setup`.")
        return
    run("INSERT OR REPLACE INTO digest VALUES (?,?,?,NULL)", (interaction.user.id, interaction.guild.id, hour))
    await interaction.followup.send(
        f"Daily digest is on. New matches will be posted in #new-matches from {hour:02d}:00 Nigeria time "
        "(within a few minutes of then, or after the bot starts if it was off). Use `/digest off` to stop.")


@digest_group.command(name="off", description="Stop the daily digest")
async def digest_off(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    run("DELETE FROM digest WHERE user_id=?", (interaction.user.id,))
    await interaction.followup.send("Daily digest is off.")


@digest_group.command(name="now", description="Post your new matches in #new-matches right now")
async def digest_now(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    posted = await send_digest(interaction.user.id, interaction.guild.id)
    await interaction.followup.send("Posted in #new-matches." if posted else
                                    "Nothing new right now, or your skills or #new-matches aren't set up.")


tree.add_command(digest_group)


# --------------------------------------------------------------- /check posting
KNOWN = ["python", "javascript", "typescript", "react", "node", "sql", "aws", "docker", "kubernetes", "java", "php",
         "ruby", "go", "rust", "c++", "figma", "photoshop", "illustrator", "seo", "copywriting", "content writing",
         "excel", "tableau", "power bi", "data analysis", "machine learning", "wordpress", "shopify", "salesforce",
         "hubspot", "zendesk", "customer support", "project management", "sales", "marketing", "video editing",
         "social media", "accounting", "quickbooks", "translation", "virtual assistant", "ui design", "ux research",
         "devops", "linux", "git", "legal research", "policy research", "research", "proofreading", "editing",
         "data entry", "fact-checking", "report writing", "ai tools", "bug reporting", "transcription"]
PAY = re.compile(r"(\$|€|£|₦|usd|eur|gbp|ngn)\s?\d[\d,.]*\s?(k|m)?(\s?(-|–|to)\s?(\$|€|£)?\s?\d[\d,.]*\s?(k|m)?)?"
                 r"(\s?(/|per|an|a)\s?(hour|hr|year|yr|month|week|day|annum))?", re.I)
REMOTE = re.compile(r"remote|work from home|anywhere", re.I)
FLAGS = [
    (re.compile(r"registration fee|training fee|pay (a |an )?(fee|deposit)|deposit required", re.I), "Asks you to pay money up front"),
    (re.compile(r"gift cards?|wire transfer|western union|cash(ier)?'?s? che(c)?k|reimburse.*(equipment|check)", re.I),
     "Mentions gift cards, wire transfers or cheques, a common scam pattern"),
    (re.compile(r"telegram|whatsapp (only|number)|text .* to apply", re.I), "Moves you to Telegram or WhatsApp to apply"),
    (re.compile(r"no experience (needed|necessary|required)", re.I), "“No experience needed” on a paid role, worth a second look"),
    (re.compile(r"unlimited earning|guaranteed income|be your own boss|earn \$?\d+[\d,]*\s*(/|per)\s*(day|hour|week)", re.I),
     "Vague or guaranteed earnings claims"),
    (re.compile(r"crypto|forex|investment opportunity", re.I), "Crypto or investment language in a job post"),
]
LOC_TEXT = {"yes": "Open to worldwide or Nigerian applicants ✓",
            "maybe": "Location not clearly stated. Check the posting for residency rules.",
            "no": "Looks restricted to other countries. Check before applying."}


def analyze_posting(text, skills):
    mine = [s for s in skills if jf.has_skill(text, s)]
    missing = [k for k in KNOWN if jf.has_skill(text, k) and k not in skills]
    denom = len(mine) + len(missing)
    return {
        "mine": mine, "missing": missing,
        "pct": round(len(mine) / denom * 100) if denom else None,
        "pay": (PAY.search(text) or [None])[0], "remote": bool(REMOTE.search(text)),
        "flags": [msg for rx, msg in FLAGS if rx.search(text)],
        "loc": jf.location_status({"location": "", "text": text}),
    }


def check_embed(text, skills, company):
    a = analyze_posting(text, skills)
    score = "No specific skills detected in this posting" if a["pct"] is None else f"{a['pct']}% of the skills it asks for"
    embed = discord.Embed(title="Posting check", description=f"**{score}**", color=WARN_COLOR if a["flags"] else COLOR)
    embed.add_field(name="Skills you have that it mentions", value=(", ".join(a["mine"]) or "None found")[:1000], inline=False)
    embed.add_field(name="Skills it asks for that you haven't listed", value=(", ".join(a["missing"]) or "None detected")[:1000], inline=False)
    embed.add_field(name="Pay", value=f"Mentioned: {a['pay'].strip()}" if a["pay"] else "No pay figure found. Ask before you invest time.", inline=False)
    embed.add_field(name="Remote", value="Says remote or work from home" if a["remote"] else "Doesn't clearly say remote", inline=False)
    embed.add_field(name="Location", value=LOC_TEXT[a["loc"]], inline=False)
    embed.add_field(name="Warning signs",
                    value=("\n".join("• " + f for f in a["flags"]) if a["flags"] else
                           "None detected. Still confirm the company exists before sharing personal details.")[:1000],
                    inline=False)
    if company.strip():
        key = norm_company(company)
        reasons = run("SELECT reason FROM reports WHERE company_key=? ORDER BY created DESC LIMIT 3", (key,), fetch=True)
        total = report_counts().get(key, 0)
        embed.add_field(
            name="Community reports",
            value=(f"⚠ {total} member{'s' if total > 1 else ''} reported {clean(company, 40)}:\n"
                   + "\n".join("• " + clean(r[0], 150) for r in reasons)) if total else "No reports for this company.",
            inline=False)
    if not skills:
        embed.set_footer(text="Add your skills with /skills add to get a match score.")
    return embed


class CheckModal(discord.ui.Modal, title="Check a job posting"):
    company = discord.ui.TextInput(label="Company (optional)", required=False, max_length=80)
    posting = discord.ui.TextInput(label="Paste the job posting", style=discord.TextStyle.paragraph, max_length=4000)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=check_embed(str(self.posting), get_skills(interaction.user.id), str(self.company)), ephemeral=True)


@tree.command(name="check", description="Check a job posting for fit, pay and scam warning signs")
async def check(interaction: discord.Interaction):
    await interaction.response.send_modal(CheckModal())


# ---------------------------------------------------------------------- /note
@tree.command(name="note", description="Draft a short application note")
@app_commands.describe(company="Company name", role="Job title",
                       focus="Skills to highlight, separated by commas (default: your first three)",
                       found_on="Where you saw the job, e.g. Remotive (adds a 'how did you find us' line)")
async def note(interaction: discord.Interaction, company: str, role: str, focus: str = "", found_on: str = ""):
    await interaction.response.defer(ephemeral=True)
    mine = get_skills(interaction.user.id)
    chosen = [s.strip().lower() for s in focus.split(",") if s.strip()][:4] or mine[:3]
    intro = get_prefs(interaction.user.id)["intro"]
    skills_line = (f"My main strengths for this work are {', '.join(chosen)}. " if chosen else "")
    text = (f"Hello {company.strip()} hiring team,\n\n"
            f"I'm applying for the {role.strip()} role. {intro + ' ' if intro else ''}{skills_line}"
            "I'm comfortable working remotely and communicating in writing, and I can share samples of my work.\n\n"
            "Could you confirm the pay range and the next step in your process?\n\nThank you,\n[Your name]")
    reply = f"```\n{text}\n```\nEdit anything that isn't true for you before sending."
    if found_on.strip():
        reply += (f"\n\nIf the form asks how you found the job:\n```\nI found the role on {found_on.strip()} while "
                  f"searching for remote {role.strip()} positions.\n```")
    await interaction.followup.send(reply[:1990])


# ---------------------------------------------------------- application tracker
StatusName = Literal["Saved", "Applied", "Interview", "Offer", "Rejected"]
STATUS_ORDER = ["Saved", "Applied", "Interview", "Offer", "Rejected"]


@tree.command(name="apply", description="Log a job you applied for (or saved)")
@app_commands.describe(company="Company name", role="Job title",
                       link="Link to the posting (optional)", status="Where you are with it (default: Applied)")
async def apply(interaction: discord.Interaction, company: str, role: str, link: str = "",
                status: StatusName = "Applied"):
    await interaction.response.defer(ephemeral=True)
    link = link.strip()
    if link and not re.match(r"https?://", link, re.I):
        await interaction.followup.send("The link should start with http:// or https://. Run `/apply` again.")
        return
    num, is_new = add_application(interaction.user.id, interaction.guild_id, company, role, link, status)
    if not is_new:
        await interaction.followup.send(f"That link is already in your list as **#{num}**. Use `/status` to update it.")
        return
    reminder = " I'll remind you to follow up in a week." if status == "Applied" else ""
    await interaction.followup.send(f"Saved as **#{num}**: {clean(role, 60)} at {clean(company, 40)} ({status}).{reminder}")


@tree.command(name="applications", description="See the jobs you have logged")
@app_commands.describe(status="Only show this status (default: all)")
async def applications(interaction: discord.Interaction,
                       status: Literal["All", "Saved", "Applied", "Interview", "Offer", "Rejected"] = "All"):
    await interaction.response.defer(ephemeral=True)
    rows = run("SELECT num, company, role, url, status, updated FROM applications WHERE user_id=? ORDER BY num DESC",
               (interaction.user.id,), fetch=True)
    if not rows:
        await interaction.followup.send("Nothing logged yet. Add one with `/apply`, or click Save under a job.")
        return
    totals = {st: sum(1 for r in rows if r[4] == st) for st in STATUS_ORDER}
    summary = " · ".join(f"{st} {n}" for st, n in totals.items() if n)
    lines = []
    for num, company, role, url, st, updated in [r for r in rows if status == "All" or r[4] == status][:25]:
        link = f" · [open]({url.replace('(', '%28').replace(')', '%29')})" if url else ""
        lines.append(f"**#{num}** {clean(role, 60)} at {clean(company, 40)}\n{st} · updated {updated}{link}")
    embed = discord.Embed(title="Your applications", color=COLOR,
                          description=("\n\n".join(lines) or "None with that status.")[:4000])
    embed.set_footer(text=summary)
    await interaction.followup.send(embed=embed)


@tree.command(name="status", description="Update an application, or remove it from your list")
@app_commands.describe(number="The # shown in /applications", new_status="New status, or Remove")
async def status(interaction: discord.Interaction, number: int,
                 new_status: Literal["Saved", "Applied", "Interview", "Offer", "Rejected", "Remove"]):
    await interaction.response.defer(ephemeral=True)
    found = run("SELECT role, company FROM applications WHERE user_id=? AND num=?", (interaction.user.id, number), fetch=True)
    if not found:
        await interaction.followup.send(f"I can't find #{number}. Check the numbers with `/applications`.")
        return
    role, company = found[0]
    if new_status == "Remove":
        run("DELETE FROM applications WHERE user_id=? AND num=?", (interaction.user.id, number))
        await interaction.followup.send(f"Removed #{number} ({clean(role, 60)} at {clean(company, 40)}).")
        return
    run("UPDATE applications SET status=?, updated=?, remind=?, reminded=0 WHERE user_id=? AND num=?",
        (new_status, today_ng(), iso_in(7) if new_status == "Applied" else None, interaction.user.id, number))
    await interaction.followup.send(f"#{number} ({clean(role, 60)} at {clean(company, 40)}) is now **{new_status}**.")


@tasks.loop(minutes=30)
async def reminder_loop():
    today = datetime.now(NIGERIA).strftime("%Y-%m-%d")
    rows = run("""SELECT user_id, num, guild_id, company, role FROM applications
                  WHERE status='Applied' AND remind IS NOT NULL AND remind<=? AND COALESCE(reminded,0)=0""",
               (today,), fetch=True)
    for user_id, num, guild_id, company, role in rows:
        text = (f"Follow-up reminder: it's been a week since you applied for **{clean(role, 60)}** at "
                f"**{clean(company, 40)}** (#{num}). A short, polite follow-up email can help. "
                "Use `/status` to update it, e.g. to Interview or Rejected.")
        try:
            user = await bot.fetch_user(user_id)
            await user.send(text)
        except Exception:  # DMs closed: fall back to #applications
            try:
                row = run("SELECT channel_id FROM channels WHERE guild_id=? AND name='applications'", (guild_id,), fetch=True)
                if row:
                    channel = bot.get_channel(row[0][0]) or await bot.fetch_channel(row[0][0])
                    await channel.send(f"<@{user_id}> {text}")
            except Exception as exc:
                print(f"[reminder] could not notify {user_id}: {exc}")
        run("UPDATE applications SET reminded=1 WHERE user_id=? AND num=?", (user_id, num))


@reminder_loop.before_loop
async def before_reminders():
    await bot.wait_until_ready()


# --------------------------------------------------------------------- /prefs
prefs_group = app_commands.Group(name="prefs", description="Your personal job search settings")


@prefs_group.command(name="show", description="Show your settings")
async def prefs_show(interaction: discord.Interaction):
    p = get_prefs(interaction.user.id)
    text = (f"**Entry-level only:** {'yes' if p['entry'] else 'no'}\n"
            f"**Location filter:** {p['location']}\n"
            f"**Private /jobs results:** {'yes' if p['private'] else 'no'}\n"
            f"**Only jobs that state pay:** {'yes' if p['pay_only'] else 'no'}\n"
            f"**Excluded words:** {', '.join(p['exclude']) or 'none'}\n"
            f"**Intro line for /note:** {p['intro'] or 'none'}")
    await interaction.response.send_message(text, ephemeral=True)


@prefs_group.command(name="set", description="Change your settings (leave a field empty to keep it)")
@app_commands.describe(entry_level="Hide senior roles and jobs asking 3+ years",
                       location="How strictly to filter by where you can work",
                       private="Make /jobs results visible only to you",
                       pay_stated_only="Only show jobs that state pay")
async def prefs_set(interaction: discord.Interaction, entry_level: Optional[bool] = None,
                    location: Optional[Literal["hide restricted", "worldwide only", "show all"]] = None,
                    private: Optional[bool] = None, pay_stated_only: Optional[bool] = None):
    uid = interaction.user.id
    if entry_level is not None:
        set_pref(uid, "entry", int(entry_level))
    if location is not None:
        set_pref(uid, "location", location)
    if private is not None:
        set_pref(uid, "private", int(private))
    if pay_stated_only is not None:
        set_pref(uid, "pay_only", int(pay_stated_only))
    await interaction.response.send_message("Saved. Use `/prefs show` to check.", ephemeral=True)


@prefs_group.command(name="exclude", description="Words to hide from results, separated by commas (or 'clear')")
@app_commands.describe(words="For example: sales, cold calling, commission (type clear to reset)")
async def prefs_exclude(interaction: discord.Interaction, words: str):
    clean_words = "" if words.strip().lower() == "clear" else ",".join(
        w.strip().lower() for w in words.split(",") if w.strip())[:300]
    set_pref(interaction.user.id, "exclude", clean_words)
    await interaction.response.send_message(f"Excluded words: {clean_words.replace(',', ', ') or 'none'}", ephemeral=True)


@prefs_group.command(name="intro", description="A one-line intro used in /note (or 'clear')")
@app_commands.describe(text="For example: I'm a recent law graduate with legal research training")
async def prefs_intro(interaction: discord.Interaction, text: str):
    value = "" if text.strip().lower() == "clear" else text.strip()[:200]
    set_pref(interaction.user.id, "intro", value)
    await interaction.response.send_message(f"Intro line: {value or 'none'}", ephemeral=True)


tree.add_command(prefs_group)


# -------------------------------------------------------------------- /report
report_group = app_commands.Group(name="report", description="Warn others about a suspicious employer")


@report_group.command(name="add", description="Report a company as a scam or bad employer")
@app_commands.describe(company="Company name", reason="What happened, e.g. asked me to pay a fee")
async def report_add(interaction: discord.Interaction, company: str, reason: str):
    await interaction.response.defer(ephemeral=True)
    key = norm_company(company)
    if not key:
        await interaction.followup.send("Please give the company name.")
        return
    run("INSERT OR REPLACE INTO reports VALUES (?,?,?,?,?)",
        (key, company.strip()[:80], interaction.user.id, reason.strip()[:300], today_ng()))
    await interaction.followup.send(
        "Thanks. Your report is saved, and the company will be flagged in job results and `/check`. "
        "Reports are warnings from members, not proof.")


@report_group.command(name="lookup", description="See reports about a company")
@app_commands.describe(company="Company name")
async def report_lookup(interaction: discord.Interaction, company: str):
    await interaction.response.defer(ephemeral=True)
    rows = run("SELECT reason, created FROM reports WHERE company_key=? ORDER BY rowid DESC LIMIT 5",
               (norm_company(company),), fetch=True)
    if not rows:
        await interaction.followup.send(f"No reports for {clean(company, 40)}. That isn't proof it's safe.")
        return
    await interaction.followup.send(f"⚠ Reports about {clean(company, 40)}:\n"
                                    + "\n".join(f"• {clean(r, 200)} ({d})" for r, d in rows))


@report_group.command(name="remove", description="Remove your report (admins: remove all reports for a company)")
@app_commands.describe(company="Company name")
async def report_remove(interaction: discord.Interaction, company: str):
    await interaction.response.defer(ephemeral=True)
    key = norm_company(company)
    perms = getattr(interaction.user, "guild_permissions", None)
    if perms is not None and perms.manage_guild:
        run("DELETE FROM reports WHERE company_key=?", (key,))
        await interaction.followup.send("Removed all reports for that company.")
    else:
        run("DELETE FROM reports WHERE company_key=? AND user_id=?", (key, interaction.user.id))
        await interaction.followup.send("Removed your report for that company, if you had one.")


tree.add_command(report_group)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is missing. Add it to your .env file.")
    bot.run(TOKEN)
