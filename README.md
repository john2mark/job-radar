# Job Radar

A Discord bot that helps job seekers find remote jobs they can actually apply for. You save your skills, and the bot searches several remote-job sources at once, hides senior roles and jobs restricted to other countries, ranks what is left by how well each job matches your skills, checks postings for scam warning signs, and tracks your applications.

I built it for my own job search as a new graduate applying from Nigeria, so the defaults favour entry-level roles that are open to worldwide applicants.

## Built with AI assistance

This project was built with an AI coding assistant (Claude, by Anthropic). I defined what the bot needed to do: the entry-level and worldwide-eligibility filtering, the application tracker, the scam checks and the daily digest. I also set up the Discord application, installed and ran the bot, and tested it. The code itself was written by the AI assistant, and I am learning to read and modify it.

Parts of the logic were tested offline with simulated data. Not every feature has been tested against live Discord and every job site, so expect rough edges.

## What it does

- **Skill-based job search** across nine sources, ranked by how many of your skills each posting mentions.
- **Entry-level filter** that hides senior titles and postings asking for 3 or more years of experience.
- **Location filter** that hides jobs restricted to other countries and tags jobs as worldwide, restricted, or location not stated.
- **Daily digest** that posts only jobs you have not been shown before.
- **Posting checker** (`/check`) that scores a pasted job description against your skills and flags pay, remote status, location and scam patterns.
- **Application tracker** with per-user numbered lists, Save buttons under results, and a follow-up reminder a week after you apply.
- **Scam reports** shared by members, shown as warnings on matching companies.
- **Application note drafts** from a template filled with your skills.
- **Per-user settings** for private results, location strictness, excluded words and more.

## How it works

There is no AI model inside the bot. Everything is rule-based:

- **Matching:** each posting is scanned for your saved skills as whole-word matches. Jobs are ranked by the number of matches, with small boosts for entry-level wording and confirmed worldwide eligibility.
- **Location eligibility:** the bot reads each source's location field when it has one. If the field is empty or generic, it scans the description for phrases such as "US only" or "work from anywhere". This is a heuristic and can be wrong, so always read the posting.
- **Scam checks:** pattern rules look for upfront fees, gift cards or wire transfers, Telegram or WhatsApp-only applications, guaranteed earnings and similar signs. These are warnings, not proof.
- **Sources** are queried in parallel, and results are cached for 30 minutes to avoid overloading free APIs.
- **Storage:** skills, applications, settings and reports are kept in a local SQLite database on the machine running the bot.

## Commands

| Command                              | What it does                                                               |
| ------------------------------------ | -------------------------------------------------------------------------- |
| `/setup`                             | Admin only. Creates the channels and posts the guide.                      |
| `/skills add, list, remove, clear`   | Manage your skills.                                                        |
| `/jobs`                              | Ranked remote jobs. Options: `role`, `entry_level`, `location`, `private`. |
| `/links`                             | LinkedIn and Reddit search links filtered for entry-level remote roles.    |
| `/digest on, off, now`               | Daily new-match digest in #new-matches.                                    |
| `/check`                             | Paste a job posting to check fit, pay, location and scam signs.            |
| `/note`                              | Draft a short application note.                                            |
| `/apply`, `/applications`, `/status` | Track applications.                                                        |
| `/prefs show, set, exclude, intro`   | Personal settings.                                                         |
| `/report add, lookup, remove`        | Flag or look up suspicious employers.                                      |
| `/help`                              | List the commands.                                                         |

## Job sources

Remotive, RemoteOK, Jobicy, Arbeitnow, Himalayas, The Muse, We Work Remotely, Working Nomads, and Reddit (only with approved API credentials).

Please credit these sites if you republish listings, and follow their terms of use. LinkedIn is **not** searched by the bot, because LinkedIn has no public job API and prohibits scraping. `/links` only builds search links that you open yourself.

## Setup (Windows)

1. Install Python 3.10 or newer (developed with 3.13) from python.org, and tick **Add python.exe to PATH**.
2. Create a Discord application at discord.com/developers/applications, add a bot, and copy its token.
3. In **OAuth2 > URL Generator**, choose the scopes `bot` and `applications.commands`, plus the permissions View Channels, Manage Channels, Send Messages, Embed Links, Read Message History and Manage Messages. Use the generated link to add the bot to your own server.
4. Copy `.env.example` to a new file named `.env` and fill in `DISCORD_TOKEN` (and `GUILD_ID`, which makes commands appear instantly).
5. Install the libraries:
   
   ```
   py -3.13 -m pip install -r requirements.txt
   ```
6. Start the bot:
   
   ```
   py -3.13 bot.py
   ```
7. In Discord, run `/setup`, then `/skills add` and `/jobs`.

To start the bot automatically when you log in, copy `start_bot.example.bat`, edit the folder path inside it, and place a shortcut to it in your Windows Startup folder (`shell:startup`).

## Limitations

- The bot is online only while the machine running it is on, unless you host it on a server.
- Save buttons stop working after the bot restarts or after 24 hours. Run `/jobs` again for fresh buttons.
- Some job sources may change their formats, and a failing source is skipped with a message in the console.
- The Reddit source needs approved API access, which may not be available to new applicants.
- Matching is keyword-based. It does not understand context.

## Privacy and safety

- Never commit your `.env` file or share your bot token. If a token leaks, reset it on the Discord developer page.
- Member reports are opinions from users, not verified facts.
- Always confirm a company is real and that pay is stated before sharing personal details or applying.

## Author

## Author

John-Mark Oreoluwa Fadipe

Independent project developed with AI coding assistance (Claude).

Not affiliated with any of the job boards listed above.
