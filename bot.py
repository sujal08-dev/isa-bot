import re
import time
import os
from datetime import datetime, timedelta

import discord
from discord.ext import commands
from discord import app_commands
from pymongo import MongoClient

# ----------------- CONFIG -----------------
import os

BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")

# IDs & server/channel settings
ALLOWED_GUILD_ID = 1388870246068260965  # your server ID
WATCH_CHANNEL_ID = 1388870247829999729  # your message channel ID
POKETWO_ID = 716390085896962058         # Pokétwo ID

# Admins
ADMIN_IDS = {768703559231340544, 1321885807442788472}  # multiple admin IDs

# Reward settings
CHAT_THRESHOLD = 100
CHAT_REWARD_BOX = "base_cube"
CATCH_THRESHOLD = 150
CATCH_REWARD_BOX = "catch_box"
GEM_CURRENCY_NAME = "PCs"

# Anti-spam
CHAT_COOLDOWN_SECONDS = 5
MIN_MSG_LENGTH = 3
DUPLICATE_IGNORE_WINDOW = 10

# MongoDB collections
DB_NAME = "DiscordRewardsDB"
USERS_COLL = "users"
SHOP_COLL = "shop"

# Poketwo catch regex
CATCH_REGEX = re.compile(
    r"Congratulations\s+<@!?(\d+)>! You caught a Level \d+ .*? \(\d+\.?\d*%\)!",
    re.IGNORECASE
)

# ---------- END CONFIG ----------

intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix='/', intents=intents)
tree = bot.tree  # for slash commands

# DB setup
db_client = MongoClient(YOUR_MONGO_URI)
db = db_client[DB_NAME]
users = db[USERS_COLL]
shop = db[SHOP_COLL]

# in-memory rate tracking
last_message_time = {}
last_message_content = {}

# ---- Helpers ----
def is_privileged(user: discord.User, guild: discord.Guild):
    if user.id in ADMIN_IDS:
        return True
    if guild is None:
        return False
    if user.id == guild.owner_id:
        return True
    member = guild.get_member(user.id)
    return member.guild_permissions.manage_guild if member else False

def get_user_data(user_id):
    user = users.find_one({"_id": user_id})
    if not user:
        new = {"_id": user_id, "balance": 0, "chat_count": 0, "catch_count": 0, "boxes": []}
        users.insert_one(new)
        return new
    return user

# ---- Bot Events ----
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id: {bot.user.id})")
    print("Bot is ready.")
    # Register slash commands for this guild
    await tree.sync(guild=discord.Object(id=ALLOWED_GUILD_ID))

# ---- Slash Commands ----
# --- Balance ---
@tree.command(name="balance", description="Check your balance", guild=discord.Object(id=ALLOWED_GUILD_ID))
@app_commands.describe(member="Check someone else's balance")
async def balance(interaction: discord.Interaction, member: discord.Member = None):
    target = member or interaction.user
    data = get_user_data(target.id)
    await interaction.response.send_message(
        f"💰 {target.mention} has **{data.get('balance',0):,} {GEM_CURRENCY_NAME}**. "
        f"Boxes: {len(data.get('boxes',[]))}. "
        f"Chat: {data.get('chat_count',0)}/{CHAT_THRESHOLD} "
        f"Catch: {data.get('catch_count',0)}/{CATCH_THRESHOLD}"
    )

# --- Claim Box ---
@tree.command(name="claimbox", description="Claim a reward box", guild=discord.Object(id=ALLOWED_GUILD_ID))
async def claimbox(interaction: discord.Interaction):
    data = get_user_data(interaction.user.id)
    if not data.get("boxes"):
        await interaction.response.send_message("You have no boxes to claim.")
        return
    box = data["boxes"].pop(0)
    reward = {"gems": 100, "pcs": 1}
    users.update_one({"_id": interaction.user.id}, {"$set": {"boxes": data["boxes"]}, "$inc": {"balance": reward["pcs"]}})
    await interaction.response.send_message(f"🔓 You opened a **{box}** and received {reward['pcs']} {GEM_CURRENCY_NAME}.")

# --- Admin Commands ---
async def admin_check(interaction: discord.Interaction):
    if not is_privileged(interaction.user, interaction.guild):
        await interaction.response.send_message("❌ You are not allowed to use this.", ephemeral=True)
        return False
    return True

@tree.command(name="addcoins", description="Add coins to a member", guild=discord.Object(id=ALLOWED_GUILD_ID))
@app_commands.describe(member="Target member", amount="Amount of coins")
async def addcoins(interaction: discord.Interaction, member: discord.Member, amount: int):
    if not await admin_check(interaction): return
    users.update_one({"_id": member.id}, {"$inc": {"balance": amount}}, upsert=True)
    await interaction.response.send_message(f"✅ Added {amount} {GEM_CURRENCY_NAME} to {member.mention}.")

@tree.command(name="removecoins", description="Remove coins from a member", guild=discord.Object(id=ALLOWED_GUILD_ID))
@app_commands.describe(member="Target member", amount="Amount of coins")
async def removecoins(interaction: discord.Interaction, member: discord.Member, amount: int):
    if not await admin_check(interaction): return
    users.update_one({"_id": member.id}, {"$inc": {"balance": -amount}}, upsert=True)
    await interaction.response.send_message(f"✅ Removed {amount} {GEM_CURRENCY_NAME} from {member.mention}.")

@tree.command(name="setcoins", description="Set coins for a member", guild=discord.Object(id=ALLOWED_GUILD_ID))
@app_commands.describe(member="Target member", amount="Amount of coins")
async def setcoins(interaction: discord.Interaction, member: discord.Member, amount: int):
    if not await admin_check(interaction): return
    users.update_one({"_id": member.id}, {"$set": {"balance": amount}}, upsert=True)
    await interaction.response.send_message(f"✅ Set {member.mention}'s balance to {amount} {GEM_CURRENCY_NAME}.")

@tree.command(name="givebox", description="Give a box to a member", guild=discord.Object(id=ALLOWED_GUILD_ID))
@app_commands.describe(member="Target member", box_type="Box type")
async def givebox(interaction: discord.Interaction, member: discord.Member, box_type: str):
    if not await admin_check(interaction): return
    users.update_one({"_id": member.id}, {"$push": {"boxes": box_type}}, upsert=True)
    await interaction.response.send_message(f"🎁 Gave {box_type} to {member.mention}.")

@tree.command(name="resetcounts", description="Reset chat and catch counts for a member", guild=discord.Object(id=ALLOWED_GUILD_ID))
@app_commands.describe(member="Target member (leave empty for yourself)")
async def resetcounts(interaction: discord.Interaction, member: discord.Member = None):
    if not await admin_check(interaction): return
    target = member.id if member else interaction.user.id
    users.update_one({"_id": target}, {"$set": {"chat_count": 0, "catch_count": 0}}, upsert=True)
    await interaction.response.send_message(f"🔁 Reset counts for <@{target}>.")

# ---- Message Listener ----
@bot.event
async def on_message(message):
    if message.author.bot:
        return
    if message.guild and message.guild.id != ALLOWED_GUILD_ID:
        return

    await bot.process_commands(message)

    if not message.guild or message.channel.id != WATCH_CHANNEL_ID:
        return

    user_id = message.author.id
    now = time.time()

    if len(message.content.strip()) < MIN_MSG_LENGTH:
        return

    last_content = last_message_content.get(user_id)
    last_time = last_message_time.get(user_id, 0)
    if last_content and message.content.strip() == last_content and (now - last_time) < DUPLICATE_IGNORE_WINDOW:
        return

    if (now - last_time) < CHAT_COOLDOWN_SECONDS:
        last_message_time[user_id] = now
        last_message_content[user_id] = message.content.strip()
        return

    users.update_one({"_id": user_id}, {"$inc": {"chat_count": 1}}, upsert=True)
    last_message_time[user_id] = now
    last_message_content[user_id] = message.content.strip()

    data = get_user_data(user_id)
    if data['chat_count'] >= CHAT_THRESHOLD:
        users.update_one(
            {"_id": user_id},
            {"$push": {"boxes": CHAT_REWARD_BOX}, "$inc": {"chat_count": -CHAT_THRESHOLD}},
            upsert=True
        )
        await message.channel.send(
            f"🎉 {message.author.mention} earned a **{CHAT_REWARD_BOX}** for sending {CHAT_THRESHOLD} messages!"
        )

    if message.author.id == POKETWO_ID:
        m = CATCH_REGEX.search(message.content)
        if m:
            try:
                catcher_id = int(m.group(1))
                users.update_one({"_id": catcher_id}, {"$inc": {"catch_count": 1}}, upsert=True)
                catcher_data = get_user_data(catcher_id)
                if catcher_data['catch_count'] >= CATCH_THRESHOLD:
                    users.update_one(
                        {"_id": catcher_id},
                        {"$push": {"boxes": CATCH_REWARD_BOX}, "$inc": {"catch_count": -CATCH_THRESHOLD}},
                        upsert=True
                    )
                    await message.channel.send(
                        f"🎁 <@{catcher_id}> earned a **{CATCH_REWARD_BOX}** for {CATCH_THRESHOLD} catches!"
                    )
            except Exception as e:
                print("Error processing catch:", e)

# ---- Run Bot ----
if __name__ == "__main__":
    bot.run(YOUR_BOT_TOKEN)
