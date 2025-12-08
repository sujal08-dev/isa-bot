# bot.py
import os
import re
import time
import asyncio
from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands
from pymongo import MongoClient

# ----------------- CONFIG (ENV) -----------------
# Set these in your Render / Replit / Railway environment variables
BOT_TOKEN = os.getenv("BOT_TOKEN")       # required
MONGO_URI = os.getenv("MONGO_URI")       # required

# Basic safety: fail early if env not set
if not BOT_TOKEN:
    raise RuntimeError("Environment variable BOT_TOKEN is not set.")
if not MONGO_URI:
    raise RuntimeError("Environment variable MONGO_URI is not set.")

# IDs & server/channel settings (replace with your values)
ALLOWED_GUILD_ID = 1388870246068260965
WATCH_CHANNEL_ID = 1388870247829999729
POKETWO_ID = 716390085896962058

# Admins (set IDs)
ADMIN_IDS = {768703559231340544, 1321885807442788472}

# Rewards & thresholds
CHAT_THRESHOLD = 100
CHAT_REWARD_BOX = "base_cube"
CATCH_THRESHOLD = 150
CATCH_REWARD_BOX = "catch_box"
GEM_CURRENCY_NAME = "PCs"

# Anti-spam
CHAT_COOLDOWN_SECONDS = 5
MIN_MSG_LENGTH = 3
DUPLICATE_IGNORE_WINDOW = 10

# MongoDB names
DB_NAME = "DiscordRewardsDB"
USERS_COLL = "users"
SETTINGS_COLL = "settings"  # store guild UI mode etc.
SHOP_COLL = "shop"

# Poketwo catch regex - matches the format you gave
CATCH_REGEX = re.compile(
    r"Congratulations\s+<@!?(\d+)>! You caught a Level \d+ .*? \(\d+\.?\d*%\)!",
    re.IGNORECASE
)

# ---------- END CONFIG ----------

intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.members = True

# allow both slash + prefix fallback
bot = commands.Bot(command_prefix=['/', '!'], intents=intents)
tree = bot.tree

# Connect MongoDB (synchronous client, we'll call it via threads)
db_client = MongoClient(MONGO_URI)
db = db_client[DB_NAME]
users = db[USERS_COLL]
settings = db[SETTINGS_COLL]
shop = db[SHOP_COLL]

# in-memory rate tracking
last_message_time = {}
last_message_content = {}

# ----------------- Async DB helpers (avoid blocking event loop) -----------------
async def db_find_one(collection, filter):
    return await asyncio.to_thread(collection.find_one, filter)

async def db_insert_one(collection, doc):
    return await asyncio.to_thread(collection.insert_one, doc)

async def db_update_one(collection, filter, update, upsert=False):
    return await asyncio.to_thread(collection.update_one, filter, update, {"upsert": upsert})

async def db_find_one_and_update(collection, filter, update, upsert=False, return_doc=False):
    # basic wrapper if you need atomic-like operations
    return await asyncio.to_thread(collection.find_one_and_update, filter, update, {"upsert": upsert, "return_document": 1})

# ----------------- Helpers -----------------
def is_privileged(user: discord.User, guild: discord.Guild):
    if user.id in ADMIN_IDS:
        return True
    if guild is None:
        return False
    if user.id == guild.owner_id:
        return True
    member = guild.get_member(user.id)
    return member.guild_permissions.manage_guild if member else False

async def get_user_data(user_id: int):
    user = await db_find_one(users, {"_id": user_id})
    if not user:
        new = {"_id": user_id, "balance": 0, "chat_count": 0, "catch_count": 0, "boxes": []}
        await db_insert_one(users, new)
        return new
    return user

async def increment_user(user_id: int, field: str, amount: int = 1):
    await db_update_one(users, {"_id": user_id}, {"$inc": {field: amount}}, upsert=True)

async def push_box(user_id: int, box_type: str):
    await db_update_one(users, {"_id": user_id}, {"$push": {"boxes": box_type}}, upsert=True)

# Guild settings (for UI mode)
# modes: "classic", "kawaii", "ultimate"
async def get_guild_mode(guild_id: int):
    s = await db_find_one(settings, {"_id": guild_id})
    if s and "mode" in s:
        return s["mode"]
    # default
    return "kawaii"

async def set_guild_mode(guild_id: int, mode: str):
    await db_update_one(settings, {"_id": guild_id}, {"$set": {"mode": mode}}, upsert=True)

# ----------------- UI / Embed Builders -----------------
def avatar_url_of(member: discord.Member):
    try:
        return member.display_avatar.url
    except Exception:
        return None

def build_balance_embed(member: discord.Member, data: dict, mode: str, bot_user: discord.User):
    # returns a discord.Embed shaped by mode
    if mode == "classic":
        embed = discord.Embed(
            title=f"{member.display_name}'s Profile",
            description=f"Balance summary for {member.mention}",
            color=discord.Color.blurple()
        )
        embed.add_field(name="Balance", value=f"{data.get('balance',0):,} {GEM_CURRENCY_NAME}", inline=False)
        embed.add_field(name="Boxes", value=str(len(data.get("boxes", []))), inline=True)
        embed.add_field(name="Chat Progress", value=f"{data.get('chat_count',0)}/{CHAT_THRESHOLD}", inline=True)
        embed.set_footer(text="Isa Bot")
        if avatar_url_of(member):
            embed.set_thumbnail(url=avatar_url_of(member))
        return embed

    if mode == "ultimate":
        embed = discord.Embed(
            title=f"✨ ULTIMATE • {member.display_name}'s Dashboard",
            description=f"Congratulations, {member.mention}! Here's your leaderboard-ready profile:",
            color=discord.Color.gold()
        )
        embed.add_field(name="💰 Pokécoins (PCs)", value=f"**{data.get('balance',0):,}**", inline=False)
        embed.add_field(name="🎁 Boxes", value=f"**{len(data.get('boxes', []))}** available — use `/claimbox`", inline=False)
        embed.add_field(name="📊 Chat Progress", value=f"**{data.get('chat_count',0)}/{CHAT_THRESHOLD}** messages", inline=True)
        embed.add_field(name="⚔️ Catch Progress", value=f"**{data.get('catch_count',0)}/{CATCH_THRESHOLD}** catches", inline=True)
        embed.set_image(url="https://i.imgur.com/6Y3ZkqF.png")  # nice banner (example)
        embed.set_footer(text="Isa • Ultimate Mode", icon_url=bot_user.avatar.url if bot_user.avatar else None)
        return embed

    # default kawaii
    embed = discord.Embed(
        title=f"🌸 {member.display_name}'s Kawaii Profile",
        description=f"Hi {member.mention} — you're doing great! uwu",
        color=discord.Color.purple()
    )
    embed.add_field(name="🍪 Unclaimed Pokécoins", value=f"**{data.get('balance',0):,} {GEM_CURRENCY_NAME}**", inline=False)
    embed.add_field(name="🎁 Magic Cubes", value=f"**{len(data.get('boxes', []))}**", inline=True)
    embed.add_field(name="💬 Chat Progress", value=f"{data.get('chat_count',0)}/{CHAT_THRESHOLD}", inline=True)
    embed.add_field(name="🧲 Catch Progress", value=f"{data.get('catch_count',0)}/{CATCH_THRESHOLD}", inline=True)
    # cute footer & thumbnail
    if avatar_url_of(member):
        embed.set_thumbnail(url=avatar_url_of(member))
    embed.set_footer(text="Isa • Kawaii Mode • ✨", icon_url=bot.user.avatar.url if bot.user.avatar else None)
    return embed

def build_claim_embed(member: discord.Member, box: str, reward: dict, boxes_left: int, mode: str):
    if mode == "ultimate":
        embed = discord.Embed(
            title=f"🎉 {member.display_name} opened a {box.replace('_',' ').title()}!",
            description=f"**You got:**",
            color=discord.Color.gold()
        )
        embed.add_field(name="💎 Gems", value=str(reward.get("gems", 0)), inline=True)
        embed.add_field(name="🪙 PCs", value=str(reward.get("pcs", 0)), inline=True)
        embed.add_field(name="📦 Remaining Boxes", value=str(boxes_left), inline=False)
        embed.set_footer(text="Isa • Ultimate Mode")
        return embed

    if mode == "classic":
        embed = discord.Embed(
            title=f"Box opened: {box.replace('_',' ').title()}",
            description=f"You received {reward.get('pcs',0)} {GEM_CURRENCY_NAME} (+{reward.get('gems',0)} gems).",
            color=discord.Color.blue()
        )
        embed.set_footer(text="Isa Bot")
        return embed

    # kawaii
    embed = discord.Embed(
        title=f"📦 You opened a {box.replace('_',' ').title()}! ✨",
        description=f"yay {member.mention} — look what you got! (｡•̀ᴗ-)✧",
        color=discord.Color.magenta()
    )
    embed.add_field(name="🍬 Pokécoins", value=str(reward.get("pcs",0)), inline=True)
    embed.add_field(name="🌟 Gems", value=str(reward.get("gems",0)), inline=True)
    embed.add_field(name="📦 Boxes Left", value=str(boxes_left), inline=False)
    embed.set_footer(text="Isa • Kawaii Mode • keep collecting! uwu")
    return embed

# ----------------- Bot Events -----------------
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id: {bot.user.id})")
    print("Bot is ready.")
    # Register slash commands in the specified guild (fast immediate registration)
    try:
        await tree.sync(guild=discord.Object(id=ALLOWED_GUILD_ID))
        print("Slash commands synced to guild.")
    except Exception as e:
        print("Failed to sync commands:", e)

# ----------------- Slash Commands -----------------
# balance
@tree.command(name="balance", description="Check your balance", guild=discord.Object(id=ALLOWED_GUILD_ID))
@app_commands.describe(member="Check someone else's balance")
async def slash_balance(interaction: discord.Interaction, member: discord.Member = None):
    target = member or interaction.user
    data = await get_user_data(target.id)
    mode = await get_guild_mode(interaction.guild_id or ALLOWED_GUILD_ID)
    embed = build_balance_embed(target, data, mode, bot.user)
    await interaction.response.send_message(embed=embed)

# claimbox
@tree.command(name="claimbox", description="Open one of your boxes", guild=discord.Object(id=ALLOWED_GUILD_ID))
async def slash_claimbox(interaction: discord.Interaction):
    data = await get_user_data(interaction.user.id)
    if not data.get("boxes"):
        mode = await get_guild_mode(interaction.guild_id or ALLOWED_GUILD_ID)
        embed = discord.Embed(title="📭 No boxes!", description="You don't have any boxes right now. Keep chatting or catching to earn some!", color=discord.Color.red() if mode=="classic" else discord.Color.dark_magenta())
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    box = data["boxes"].pop(0)
    # example rewards — tune later or make probability tables
    reward = {"gems": 60, "pcs": 60000} if box == "fire_cube" else {"gems": 100, "pcs": 1}
    # update db
    await db_update_one(users, {"_id": interaction.user.id}, {"$set": {"boxes": data["boxes"]}, "$inc": {"balance": reward["pcs"]}}, upsert=True)
    mode = await get_guild_mode(interaction.guild_id or ALLOWED_GUILD_ID)
    embed = build_claim_embed(interaction.user, box, reward, len(data["boxes"]), mode)
    await interaction.response.send_message(embed=embed)

# admin helper
async def admin_check_and_reply(interaction: discord.Interaction):
    if not is_privileged(interaction.user, interaction.guild):
        await interaction.response.send_message("❌ You lack permission to use this.", ephemeral=True)
        return False
    return True

# addcoins
@tree.command(name="addcoins", description="Add Pokécoins to a member", guild=discord.Object(id=ALLOWED_GUILD_ID))
@app_commands.describe(member="Target member", amount="Amount to add")
async def slash_addcoins(interaction: discord.Interaction, member: discord.Member, amount: int):
    if not await admin_check_and_reply(interaction): return
    await db_update_one(users, {"_id": member.id}, {"$inc": {"balance": amount}}, upsert=True)
    await interaction.response.send_message(f"✅ Added {amount} {GEM_CURRENCY_NAME} to {member.mention}.")

@tree.command(name="removecoins", description="Remove Pokécoins from a member", guild=discord.Object(id=ALLOWED_GUILD_ID))
@app_commands.describe(member="Target member", amount="Amount to remove")
async def slash_removecoins(interaction: discord.Interaction, member: discord.Member, amount: int):
    if not await admin_check_and_reply(interaction): return
    await db_update_one(users, {"_id": member.id}, {"$inc": {"balance": -amount}}, upsert=True)
    await interaction.response.send_message(f"✅ Removed {amount} {GEM_CURRENCY_NAME} from {member.mention}.")

@tree.command(name="setcoins", description="Set Pokécoins for a member", guild=discord.Object(id=ALLOWED_GUILD_ID))
@app_commands.describe(member="Target member", amount="New balance")
async def slash_setcoins(interaction: discord.Interaction, member: discord.Member, amount: int):
    if not await admin_check_and_reply(interaction): return
    await db_update_one(users, {"_id": member.id}, {"$set": {"balance": amount}}, upsert=True)
    await interaction.response.send_message(f"✅ Set {member.mention}'s balance to {amount} {GEM_CURRENCY_NAME}.")

@tree.command(name="givebox", description="Give a box to a member", guild=discord.Object(id=ALLOWED_GUILD_ID))
@app_commands.describe(member="Target member", box_type="Box type e.g. base_cube or fire_cube")
async def slash_givebox(interaction: discord.Interaction, member: discord.Member, box_type: str):
    if not await admin_check_and_reply(interaction): return
    await db_update_one(users, {"_id": member.id}, {"$push": {"boxes": box_type}}, upsert=True)
    await interaction.response.send_message(f"🎁 Gave **{box_type}** to {member.mention}.")

@tree.command(name="resetcounts", description="Reset chat & catch counts for a member (or yourself)", guild=discord.Object(id=ALLOWED_GUILD_ID))
@app_commands.describe(member="Target member (leave empty for yourself)")
async def slash_resetcounts(interaction: discord.Interaction, member: discord.Member = None):
    if not await admin_check_and_reply(interaction): return
    target = member.id if member else interaction.user.id
    await db_update_one(users, {"_id": target}, {"$set": {"chat_count": 0, "catch_count": 0}}, upsert=True)
    await interaction.response.send_message(f"🔁 Reset counts for <@{target}>.")

# setmode (UI)
@tree.command(name="setmode", description="Set server UI mode: classic / kawaii / ultimate", guild=discord.Object(id=ALLOWED_GUILD_ID))
@app_commands.describe(mode="classic, kawaii, or ultimate")
async def slash_setmode(interaction: discord.Interaction, mode: str):
    if not await admin_check_and_reply(interaction): return
    mode = mode.lower()
    if mode not in {"classic", "kawaii", "ultimate"}:
        await interaction.response.send_message("Mode must be one of: classic, kawaii, ultimate", ephemeral=True)
        return
    await set_guild_mode(interaction.guild_id or ALLOWED_GUILD_ID, mode)
    await interaction.response.send_message(f"✅ Server UI mode set to **{mode}**.")

# ----------------- Message Listener (counts & catches) -----------------
@bot.event
async def on_message(message: discord.Message):
    # ignore bots
    if message.author.bot:
        return

    # restrict to configured server (optional)
    if message.guild and message.guild.id != ALLOWED_GUILD_ID:
        return

    # allow commands to process first
    await bot.process_commands(message)

    # only count messages in watch channel
    if not message.guild or message.channel.id != WATCH_CHANNEL_ID:
        return

    user_id = message.author.id
    now = time.time()

    # basic anti-spam filters
    if not message.content or len(message.content.strip()) < MIN_MSG_LENGTH:
        return

    last_content = last_message_content.get(user_id)
    last_time = last_message_time.get(user_id, 0)
    if last_content and message.content.strip() == last_content and (now - last_time) < DUPLICATE_IGNORE_WINDOW:
        return

    if (now - last_time) < CHAT_COOLDOWN_SECONDS:
        last_message_time[user_id] = now
        last_message_content[user_id] = message.content.strip()
        return

    # increment chat_count in DB (async-safe)
    await db_update_one(users, {"_id": user_id}, {"$inc": {"chat_count": 1}}, upsert=True)
    last_message_time[user_id] = now
    last_message_content[user_id] = message.content.strip()

    data = await get_user_data(user_id)
    if data.get("chat_count", 0) >= CHAT_THRESHOLD:
        await db_update_one(users, {"_id": user_id}, {"$push": {"boxes": CHAT_REWARD_BOX}, "$inc": {"chat_count": -CHAT_THRESHOLD}}, upsert=True)
        # notify in channel
        mode = await get_guild_mode(message.guild.id)
        if mode == "ultimate":
            await message.channel.send(f"🎉 **{message.author.mention}** earned a **{CHAT_REWARD_BOX}** for sending {CHAT_THRESHOLD} messages! ✨")
        else:
            await message.channel.send(f"🎉 {message.author.mention} earned a **{CHAT_REWARD_BOX}** for sending {CHAT_THRESHOLD} messages!")

    # Poketwo catches: detect messages from Poketwo bot
    if message.author.id == POKETWO_ID:
        m = CATCH_REGEX.search(message.content)
        if m:
            try:
                catcher_id = int(m.group(1))
                await db_update_one(users, {"_id": catcher_id}, {"$inc": {"catch_count": 1}}, upsert=True)
                catcher_data = await get_user_data(catcher_id)
                if catcher_data.get("catch_count", 0) >= CATCH_THRESHOLD:
                    await db_update_one(users, {"_id": catcher_id}, {"$push": {"boxes": CATCH_REWARD_BOX}, "$inc": {"catch_count": -CATCH_THRESHOLD}}, upsert=True)
                    await message.channel.send(f"🎁 <@{catcher_id}> earned a **{CATCH_REWARD_BOX}** for {CATCH_THRESHOLD} catches!")
            except Exception as e:
                print("Error processing catch:", e)

# ----------------- Run Bot -----------------
if __name__ == "__main__":
    bot.run(BOT_TOKEN)
