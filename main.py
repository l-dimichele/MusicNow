import discord
from discord.ext import commands
import yt_dlp
import asyncio
import os
from dotenv import load_dotenv
import time

# --- Load env ---
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# --- Intents ---
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="", intents=intents, heartbeat_timeout=120)

# --- YTDL / FFmpeg ---
ytdl_opts = {
    'format': 'bestaudio/best',
    'quiet': True,
    'no_warnings': True,
    'ignoreerrors': True,
    'geo_bypass': True,
    'extract_flat': True,  # velocizza il caricamento playlist
    'cookiefile': 'cookies.txt',
    'noplaylist': False,
    'default_search': 'auto'
}

ffmpeg_opts = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn',
}

ytdl = yt_dlp.YoutubeDL(ytdl_opts)

# --- Queue per guild ---
guild_states = {}
active_queue_views = {}

def get_guild_data(guild_id):
    if guild_id not in guild_states:
        guild_states[guild_id] = {
            'queue': [],
            'current_track': None,
            'voice_client': None,
            'loop': False,
            'player_message': None,
            'paused': False,
            'elapsed': 0
        }
    return guild_states[guild_id]

# --- Modifica a QueueView per registrare l'istanza ---
async def refresh_queue_embed(guild_id):
    """Aggiorna l'embed della coda se c'è un menu aperto"""
    if guild_id in active_queue_views:
        view = active_queue_views[guild_id]
        await view.update_embed()

def get_queue(guild_id):
    return get_guild_data(guild_id)['queue']

# --- YouTube API search ---
async def search_youtube_yt_dlp(query: str):
    """Cerca su YouTube senza API key usando yt-dlp"""
    loop = asyncio.get_event_loop()

    def extract():
        with yt_dlp.YoutubeDL({'quiet': True, 'extract_flat': True}) as ydl:
            # ytsearch5 prende i primi 5 risultati
            return ydl.extract_info(f"ytsearch5:{query}", download=False)

    info = await loop.run_in_executor(None, extract)
    results = []
    for entry in info.get('entries', []):
        title = entry.get('title', 'Sconosciuto')
        url = entry.get('url')
        if not url:
            continue
        # Discord OptionChoice per autocomplete
        display_title = title if len(title) <= 90 else title[:87] + "..."
        results.append(discord.OptionChoice(name=display_title, value=f"https://www.youtube.com/watch?v={entry.get('id')}"))
    return results

autocomplete_cache = {}  # semplice cache 10 sec

async def ytsearch_autocomplete(ctx: discord.AutocompleteContext):
    query = ctx.value.strip()
    if not query:
        return []

    try:
        results = await asyncio.wait_for(search_youtube_yt_dlp(query), timeout=2.5)
        return results
    except asyncio.TimeoutError:
        return []
    except Exception as e:
        print(f"Errore autocomplete yt-dlp: {e}")
        return []

# --- PlayerView ---
class PlayerView(discord.ui.View):
    def __init__(self, guild_id):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.guild_data = get_guild_data(guild_id)

        # Bottoni musica
        self.add_item(PlayResumeButton(guild_id))
        self.add_item(PauseButton(guild_id))
        self.add_item(StopButton(guild_id))
        self.add_item(NextButton(guild_id))

        # Aggiungi select solo se coda non vuota
        if self.guild_data['queue']:
            self.add_item(QueueSelectStandalone(self))

    async def refresh_queue(self):
        """Aggiorna o crea il Select della coda dinamicamente"""
        # Rimuovi vecchio Select
        for item in self.children:
            if isinstance(item, QueueSelectStandalone):
                self.remove_item(item)
        # Aggiungi nuovo Select solo se coda non vuota
        if self.guild_data['queue']:
            self.add_item(QueueSelectStandalone(self))

# --- Bottoni principali ---
class PlayResumeButton(discord.ui.Button):
    def __init__(self, guild_id):
        super().__init__(label="▶️ Play/Resume", style=discord.ButtonStyle.success)
        self.guild_id = guild_id

    async def callback(self, interaction: discord.Interaction):
        guild_data = get_guild_data(self.guild_id)
        vc = interaction.guild.voice_client
        if vc and vc.is_paused():
            vc.resume()
            guild_data['paused'] = False
            await interaction.response.send_message("▶️ Ripresa riproduzione.", ephemeral=True)
        else:
            await interaction.response.send_message("⚠️ Nulla da riprodurre.", ephemeral=True)

class PauseButton(discord.ui.Button):
    def __init__(self, guild_id):
        super().__init__(label="⏸ Pause", style=discord.ButtonStyle.secondary)
        self.guild_id = guild_id

    async def callback(self, interaction: discord.Interaction):
        guild_data = get_guild_data(self.guild_id)
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            vc.pause()
            guild_data['paused'] = True
            await interaction.response.send_message("⏸️ Musica messa in pausa.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Nessuna musica in riproduzione.", ephemeral=True)

class StopButton(discord.ui.Button):
    def __init__(self, guild_id):
        super().__init__(label="⏹ Stop", style=discord.ButtonStyle.danger)
        self.guild_id = guild_id

    async def callback(self, interaction: discord.Interaction):
        guild_data = get_guild_data(self.guild_id)
        guild_data['queue'].clear()
        guild_data['current_track'] = None
        guild_data['paused'] = False
        vc = interaction.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()

        # Cancella il messaggio del player
        if guild_data['player_message']:
            try:
                await guild_data['player_message'].delete()
            except:
                pass
            guild_data['player_message'] = None

        await interaction.response.send_message("⏹️ Riproduzione fermata e coda svuotata.", ephemeral=True)

class NextButton(discord.ui.Button):
    def __init__(self, guild_id):
        super().__init__(label="⏭ Next", style=discord.ButtonStyle.primary)
        self.guild_id = guild_id

    async def callback(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()
        await interaction.response.send_message("⏭ Passata alla traccia successiva.", ephemeral=True)

# --- Select per eliminare tracce ---
class QueueSelectStandalone(discord.ui.Select):
    def __init__(self, parent):
        self.parent = parent
        options = [
            discord.SelectOption(label=f"{i+1}. {t['title'][:90]}", value=str(i))
            for i, t in enumerate(parent.guild_data['queue'])
        ]
        super().__init__(placeholder="Seleziona traccia da eliminare", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        idx = int(self.values[0])
        removed = self.parent.guild_data['queue'].pop(idx)
        await interaction.response.send_message(f"❌ Rimosso dalla coda: **{removed['title']}**", ephemeral=True)
        # Aggiorna Select nella view
        await self.parent.refresh_queue()
        await update_player_message(interaction.guild)

# --- Funzioni principali ---
async def update_player_message(guild):
    guild_data = get_guild_data(guild.id)
    current = guild_data['current_track']
    if not current:
        return

    embed = discord.Embed(
        title="🎶 In riproduzione",
        description=current['title'],
        color=0x1DB954
    )
    if current.get('thumbnail'):
        embed.set_thumbnail(url=current['thumbnail'])

    # Ricrea sempre la View con la coda aggiornata
    view = PlayerView(guild.id)
    await view.refresh_queue()

    if guild_data['player_message']:
        try:
            await guild_data['player_message'].edit(embed=embed, view=view)
        except:
            guild_data['player_message'] = None
    else:
        channel = next(
            (ch for ch in guild.text_channels if ch.permissions_for(guild.me).send_messages),
            None
        )
        if channel:
            msg = await channel.send(embed=embed, view=view)
            guild_data['player_message'] = msg

async def ensure_vc_connected(guild, voice_channel):
    try:
        vc = guild.voice_client
        if not vc or not vc.is_connected():
            vc = await voice_channel.connect()
        elif vc.channel != voice_channel:
            await vc.move_to(voice_channel)
        return vc
    except Exception as e:
        print(f"[Music] Errore connessione voice: {e}")
        return None

async def play_next(guild, vc=None, seek_time=0):
    guild_data = get_guild_data(guild.id)
    queue = guild_data['queue']

    if not vc:
        vc = guild.voice_client
    if not vc or not vc.is_connected():
        return

    if guild_data['loop'] and guild_data.get('current_track'):
        track = guild_data['current_track']
    elif queue:
        track = queue.pop(0)
        guild_data['current_track'] = track
    else:
        guild_data['current_track'] = None
        if guild_data['player_message']:
            try:
                await guild_data['player_message'].delete()
            except:
                pass
            guild_data['player_message'] = None

        await asyncio.sleep(60)
        if vc.is_connected() and not vc.is_playing():
            await vc.disconnect()
        return

    try:
        ydl_opts = {'format': 'bestaudio/best', 'quiet': True, 'ignoreerrors': True, 'geo_bypass': True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(track['url'], download=False)
            if not info or 'url' not in info:
                await play_next(guild, vc)
                return
            url2 = info['url']

        before_opts = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
        if seek_time > 0:
            before_opts += f" -ss {seek_time}"
        ffmpeg_opts = {'before_options': before_opts, 'options': '-vn'}
        source = await discord.FFmpegOpusAudio.from_probe(url2, **ffmpeg_opts)

        def after_playing(err):
            asyncio.run_coroutine_threadsafe(play_next(guild, vc), bot.loop)

        vc.play(source, after=after_playing)

        # Aggiorna messaggio player
        await update_player_message(guild)

    except Exception as e:
        print(f"[Music] Errore in play_next: {e}")
        await play_next(guild, vc)

# --- Slash command ---
@bot.slash_command(name="play", description="Cerca o riproduci musica da YouTube")
@discord.option("query", description="Titolo o link YouTube", autocomplete=ytsearch_autocomplete)
async def play(ctx: discord.ApplicationContext, query: str):
    await ctx.defer()

    if not ctx.author.voice or not ctx.author.voice.channel:
        return await ctx.respond("❌ Devi essere in un canale vocale!", ephemeral=True)

    guild_queue = get_queue(ctx.guild.id)
    if len(guild_queue) > 120:
        return await ctx.respond("⚠️ Coda troppo lunga.", ephemeral=True)

    added = 0
    skipped = 0
    first_track_ready = asyncio.Event()

    async def process_entry(entry):
        nonlocal added, skipped
        if not entry or 'url' not in entry:
            skipped += 1
            return
        track = {
            'title': entry.get('title', 'Sconosciuto'),
            'url': entry.get('webpage_url'),
            'thumbnail': entry.get('thumbnail')
        }
        guild_queue.append(track)
        added += 1
        await update_player_message(ctx.guild)
        first_track_ready.set()  # Segnala che almeno una traccia è pronta

    try:
        ydl_opts = {'format': 'bestaudio/best', 'quiet': True, 'ignoreerrors': True, 'geo_bypass': True}

        if query.startswith("http"):
            # Mantieni il comportamento corrente con link diretto
            loop = asyncio.get_event_loop()
            def extract():
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    return ydl.extract_info(query, download=False)
            info = await loop.run_in_executor(None, extract)
            entries = info.get('entries', [info])
            await asyncio.gather(*(process_entry(entry) for entry in entries))
        else:
            # Ricerca con yt-dlp senza API
            results = await search_youtube_yt_dlp(query)
            if not results:
                return await ctx.respond("❌ Nessun risultato trovato.", ephemeral=True)
            first_url = results[0].value  # prende il primo risultato
            loop = asyncio.get_event_loop()
            def extract():
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    return ydl.extract_info(first_url, download=False)
            info = await loop.run_in_executor(None, extract)
            await process_entry(info)

    except Exception as e:
        return await ctx.respond(f"❌ Errore caricando link o ricerca: {e}", ephemeral=True)

    await ctx.respond(f"✅ Aggiunte {added} tracce alla coda. ⚠️ Skippate {skipped} tracce protette o non disponibili.")

    # Connetti al voice channel solo quando c’è una traccia pronta
    await first_track_ready.wait()
    vc = await ensure_vc_connected(ctx.guild, ctx.author.voice.channel)
    if vc is None:
        return await ctx.followup.send("❌ Non sono riuscito a connettermi al canale vocale.", ephemeral=True)

    if not vc.is_playing():
        await play_next(ctx.guild, vc)

# --- Ready ---
@bot.event
async def on_ready():
    print(f"✅ Bot connesso come {bot.user}")

bot.run(TOKEN)
