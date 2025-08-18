import discord
from discord import app_commands, Interaction
from discord.ext import commands
import yt_dlp
import asyncio
import os
from dotenv import load_dotenv


url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
    info = ydl.extract_info(url, download=False)
    print(info)

# --- Load env ---
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# --- Intents ---
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="", intents=intents)

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
    """Cerca su YouTube senza API key usando yt-dlp e ritorna Choice già pronti"""
    loop = asyncio.get_event_loop()

    def extract():
        with yt_dlp.YoutubeDL({'quiet': True, 'extract_flat': True}) as ydl:
            return ydl.extract_info(f"ytsearch5:{query}", download=False)

    info = await loop.run_in_executor(None, extract)
    results = []

    for entry in info.get('entries', []):
        title = entry.get('title', 'Sconosciuto')
        video_id = entry.get('id')
        if not video_id:
            continue
        display_title = title if len(title) <= 100 else title[:97] + "..."
        results.append(app_commands.Choice(
            name=display_title,
            value=f"https://www.youtube.com/watch?v={video_id}"
        ))

    return results

autocomplete_cache = {}  # semplice cache 10 sec

async def ytsearch_autocomplete(interaction: Interaction, current: str):
    query = current.strip()
    if not query:
        return []

    try:
        # Ottieni direttamente una lista di Choice
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

class MusicButtons(discord.ui.Button):
  def __init__(self, guild_id):
      super().__init__(style=discord.ButtonStyle.green, label="▶️ Play/Resume")
      self.guild_id = guild_id

  async def callback(self, interaction: discord.Interaction):
      vc = interaction.guild.voice_client
      guild_data = get_guild_data(self.guild_id)
      if vc and vc.is_paused():
          vc.resume()
          guild_data['paused'] = False
          await interaction.response.send_message("▶️ Ripresa riproduzione.", ephemeral=True)
      else:
          await interaction.response.send_message("⚠️ Nulla da riprodurre.", ephemeral=True)

  async def callback(self, interaction: discord.Interaction):
      idx = int(self.values[0])
      removed = self.parent.guild_data['queue'].pop(idx)
      await interaction.response.send_message(f"❌ Rimosso dalla coda: **{removed['title']}**", ephemeral=True)
      # aggiorna la view
      await update_player_message(interaction.guild)

# --- Music Controls View ---
class MusicView(discord.ui.View):
    def __init__(self, guild_id):
        super().__init__(timeout=None)
        self.guild_id = guild_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message("❌ Devi essere in un canale vocale.", ephemeral=True)
            return False
        vc = interaction.guild.voice_client
        if not vc or vc.channel != interaction.user.voice.channel:
            await interaction.response.send_message("❌ Devi essere nel canale vocale del bot.", ephemeral=True)
            return False
        return True

    # --- Music buttons ---
    @discord.ui.button(label="▶️ Play/Resume", style=discord.ButtonStyle.success)
    async def play_resume(self, button: discord.ui.Button, interaction: discord.Interaction):
        guild_data = get_guild_data(self.guild_id)
        vc = interaction.guild.voice_client
        if vc and vc.is_paused():
            vc.resume()
            guild_data['paused'] = False
            await interaction.response.send_message("▶️ Ripresa riproduzione.", ephemeral=True)
        elif vc and not vc.is_playing():
            if guild_data['queue']:
                await play_next(interaction.guild, vc)
                await interaction.response.send_message("▶️ Riproduzione avviata.", ephemeral=True)
            else:
                await interaction.response.send_message("⚠️ La coda è vuota.", ephemeral=True)
        else:
            await interaction.response.send_message("▶️ Musica già in riproduzione.", ephemeral=True)

    @discord.ui.button(label="⏸ Pause", style=discord.ButtonStyle.secondary)
    async def pause(self, button: discord.ui.Button, interaction: discord.Interaction):
        guild_data = get_guild_data(self.guild_id)
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            vc.pause()
            guild_data['paused'] = True
            await interaction.response.send_message("⏸️ Musica messa in pausa.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Nessuna musica in riproduzione.", ephemeral=True)

    @discord.ui.button(label="⏹ Stop", style=discord.ButtonStyle.danger)
    async def stop(self, button: discord.ui.Button, interaction: discord.Interaction):
        guild_data = get_guild_data(self.guild_id)
        guild_data['queue'].clear()
        guild_data['current_track'] = None
        guild_data['paused'] = False
        vc = interaction.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()
        await interaction.response.send_message("⏹️ Riproduzione fermata e coda svuotata.", ephemeral=True)

    @discord.ui.button(label="⏭ Next", style=discord.ButtonStyle.primary)
    async def next(self, button: discord.ui.Button, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()
        await interaction.response.send_message("⏭ Passata alla traccia successiva.", ephemeral=True)

# --- View separata per la coda interattiva ---
class QueueView(discord.ui.View):
    def __init__(self, ctx):
        super().__init__(timeout=120)
        self.ctx = ctx
        self.guild_data = get_guild_data(ctx.guild.id)
        self.queue = self.guild_data['queue']
        self.message = None
        self.refresh_menu()
        active_queue_views[ctx.guild.id] = self

    def refresh_menu(self):
        self.clear_items()
        if self.queue:
            self.add_item(QueueSelect(self))

    async def update_embed(self):
        if self.message:
            description = "\n".join(f"{i+1}. {t['title']}" for i, t in enumerate(self.queue)) or "La coda è vuota."
            embed = discord.Embed(title="📜 Coda aggiornata", description=description, color=discord.Color.blurple())
            await self.message.edit(embed=embed, view=self)

    async def on_timeout(self):
        if self.ctx.guild.id in active_queue_views:
            del active_queue_views[self.ctx.guild.id]
        if self.message:
            await self.message.edit(view=None)

    async def callback(self, interaction: discord.Interaction):
        idx = int(self.values[0])
        if 0 <= idx < len(self.parent.queue):
            removed = self.parent.queue.pop(idx)
            await interaction.response.send_message(f"❌ Rimosso dalla coda: **{removed['title']}**", ephemeral=True)
            # disabilita il menu dopo la selezione
            for item in self.parent.children:
                item.disabled = True
            await self.parent.update_embed()


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
@bot.tree.command(name="play", description="Cerca o riproduci musica da YouTube")
@app_commands.describe(query="Titolo o link del brano da cercare")
@app_commands.autocomplete(query=ytsearch_autocomplete)
async def play(interaction: discord.Interaction, query: str):
    await interaction.response.defer(ephemeral=True)

    if not interaction.user.voice or not interaction.user.voice.channel:
        return await interaction.followup.send("❌ Devi essere in un canale vocale!", ephemeral=True)

    guild_queue = get_queue(interaction.guild.id)
    if len(guild_queue) > 120:
        return await interaction.followup.send("⚠️ Coda troppo lunga.", ephemeral=True)

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
        await update_player_message(interaction.guild)
        first_track_ready.set()  # Segnala che almeno una traccia è pronta

    try:
        ydl_opts = {'format': 'bestaudio/best', 'quiet': True, 'ignoreerrors': True, 'geo_bypass': True}

        if query.startswith("http"):
            loop = asyncio.get_event_loop()
            def extract():
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    return ydl.extract_info(query, download=False)
            info = await loop.run_in_executor(None, extract)
            entries = info.get('entries', [info])
            await asyncio.gather(*(process_entry(entry) for entry in entries))
        else:
            results = await search_youtube_yt_dlp(query)
            if not results:
                return await interaction.followup.send("❌ Nessun risultato trovato.", ephemeral=True)
            first_url = results[0].value
            loop = asyncio.get_event_loop()
            def extract():
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    return ydl.extract_info(first_url, download=False)
            info = await loop.run_in_executor(None, extract)
            await process_entry(info)

    except Exception as e:
        return await interaction.followup.send(f"❌ Errore caricando link o ricerca: {e}", ephemeral=True)

    await interaction.followup.send(f"✅ Aggiunte {added} tracce alla coda. ⚠️ Skippate {skipped} tracce protette o non disponibili.", ephemeral=True)

    # Connetti al voice channel solo quando c’è una traccia pronta
    await first_track_ready.wait()
    vc = await ensure_vc_connected(interaction.guild, interaction.user.voice.channel)
    if vc is None:
        return await interaction.followup.send("❌ Non sono riuscito a connettermi al canale vocale.", ephemeral=True)

    if not vc.is_playing():
        await play_next(interaction.guild, vc)


# --- Ready ---
@bot.event
async def on_ready():
    print(f"✅ Connesso come {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"🔗 {len(synced)} slash command sincronizzati")
    except Exception as e:
        print(f"Errore sync: {e}")

async def main():
    await bot.start(TOKEN)

# Avvia tutto
asyncio.run(main())
