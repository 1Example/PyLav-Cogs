from __future__ import annotations

import asyncio
import contextlib
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

import discord
from redbot.core import Config, commands
from redbot.core.i18n import Translator, cog_i18n

from pylav import logging
from pylav.core.context import PyLavContext
from pylav.events.track import TrackEndEvent
from pylav.players.player import Player
from pylav.players.query.obj import Query
from pylav.players.tracks.obj import Track
from pylav.type_hints.bot import DISCORD_BOT_TYPE, DISCORD_COG_TYPE_MIXIN

_ = Translator("PyLavYouTubeRadio", Path(__file__))

LOGGER = logging.getLogger("red.PyLav.cog.YouTubeRadio")

# Reasons that mean "the track played through to the end on its own".
# Anything else (REPLACED, STOPPED, CLEANUP) means a human intervened,
# and we must not hijack that.
NATURAL_END_REASONS = {"finished", "FINISHED"}

# How many seed video IDs we remember per guild so the radio doesn't loop
# back onto the same handful of tracks.
SEED_MEMORY = 200


@cog_i18n(_)
class PyLavYouTubeRadio(DISCORD_COG_TYPE_MIXIN):
    """Keeps playing YouTube's recommended tracks when the queue runs dry.

    Unlike PyLav's built-in autoplay, this seeds a YouTube Mix from the
    track that just finished, so what plays next is related to what you
    were actually listening to.
    """

    __version__ = "1.0.0"

    def __init__(self, bot: DISCORD_BOT_TYPE, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.bot = bot
        self._config = Config.get_conf(self, identifier=208903205982044162)
        self._config.register_guild(
            enabled=False,
            buffer=3,
        )
        self._enabled_cache: dict[int, bool] = {}
        self._buffer_cache: dict[int, int] = {}
        # PyLav clears player.history when it stops on an empty queue, so we
        # keep our own memory of what the radio has already served.
        self._played: dict[int, deque[str]] = defaultdict(lambda: deque(maxlen=SEED_MEMORY))
        self._lock: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def cog_unload(self) -> None:
        self._played.clear()

    async def is_enabled(self, guild_id: int) -> bool:
        if guild_id not in self._enabled_cache:
            self._enabled_cache[guild_id] = await self._config.guild_from_id(guild_id).enabled()
        return self._enabled_cache[guild_id]

    async def get_buffer(self, guild_id: int) -> int:
        if guild_id not in self._buffer_cache:
            self._buffer_cache[guild_id] = await self._config.guild_from_id(guild_id).buffer()
        return self._buffer_cache[guild_id]

    # ------------------------------------------------------------------
    # Seed resolution
    # ------------------------------------------------------------------

    async def _youtube_id_for(self, track: Track) -> str | None:
        """Return a YouTube video ID to seed the mix from.

        YouTube tracks hand us their identifier directly. Anything else
        (Spotify, Deezer, Apple Music) has no video ID, so we resolve it by
        searching YouTube for the title and artist and taking the top hit.
        """
        with contextlib.suppress(Exception):
            source = await track.source()
            identifier = await track.identifier()
            if source and "youtube" in source.lower() and identifier:
                return identifier

        try:
            title = await track.title()
            author = await track.author()
        except Exception:
            LOGGER.debug("Could not read metadata off the finished track")
            return None

        terms = " ".join(part for part in (title, author) if part).strip()
        if not terms:
            return None

        query = await Query.from_string(f"ytsearch:{terms}")
        response = await self.pylav.get_tracks(query, fullsearch=False)
        if not response or not response.tracks:
            LOGGER.debug("No YouTube match found for %s", terms)
            return None

        candidate = await Track.build_track(
            node=await self.pylav.node_manager.find_best_node(),
            data=response.tracks[0],
            query=None,
            requester=self.bot.user.id,
        )
        with contextlib.suppress(Exception):
            return await candidate.identifier()
        return None

    async def _fetch_mix(self, video_id: str, player: Player) -> list[Any]:
        """Load a YouTube Mix (radio) playlist seeded from a video ID."""
        url = f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}"
        query = await Query.from_string(url)
        response = await self.pylav.get_tracks(query, player=player)
        if not response or not response.tracks:
            return []
        return list(response.tracks)

    # ------------------------------------------------------------------
    # The actual hook
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_pylav_track_end_event(self, event: TrackEndEvent) -> None:
        player: Player = event.player
        if player is None or player.guild is None:
            return
        guild_id = player.guild.id

        if event.reason not in NATURAL_END_REASONS:
            return
        if not await self.is_enabled(guild_id):
            return

        async with self._lock[guild_id]:
            # PyLav has already run next() by the time this fires. If
            # something is playing or queued, the queue wasn't actually
            # empty and we should stay out of the way.
            if player.current is not None or not player.queue.empty():
                return
            if not player.is_connected:
                return

            seed_track = event.track
            if seed_track is None:
                return

            with contextlib.suppress(Exception):
                encoded = seed_track.encoded
                if encoded:
                    self._played[guild_id].append(encoded)

            video_id = await self._youtube_id_for(seed_track)
            if not video_id:
                LOGGER.debug("Could not resolve a YouTube seed for guild %s", guild_id)
                return

            candidates = await self._fetch_mix(video_id, player)
            if not candidates:
                LOGGER.debug("YouTube mix RD%s returned nothing", video_id)
                return

            already_played = set(self._played[guild_id])
            fresh = [t for t in candidates if getattr(t, "encoded", None) not in already_played]
            # If the mix is entirely stuff we've heard, fall back to the raw
            # list rather than going silent.
            pool = fresh or candidates

            wanted = max(1, await self.get_buffer(guild_id))
            chosen = pool[:wanted]
            if not chosen:
                return

            requester = player.guild.me
            for track in chosen:
                with contextlib.suppress(Exception):
                    if enc := getattr(track, "encoded", None):
                        self._played[guild_id].append(enc)

            await player.bulk_add(
                tracks_and_queries=chosen,
                requester=requester.id,
            )

            if player.current is None:
                await player.play(None, None, requester=requester)

            LOGGER.debug("Queued %s radio tracks in guild %s", len(chosen), guild_id)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    @commands.group(name="ytradio")
    @commands.guild_only()
    async def command_ytradio(self, context: PyLavContext) -> None:
        """Control YouTube radio autoplay."""

    @command_ytradio.command(name="toggle")
    @commands.admin_or_permissions(manage_guild=True)
    async def command_ytradio_toggle(self, context: PyLavContext, toggle: bool) -> None:
        """Turn YouTube radio on or off for this server."""
        if isinstance(context, discord.Interaction):
            context = await self.bot.get_context(context)
        if context.interaction and not context.interaction.response.is_done():
            await context.defer(ephemeral=True)

        await self._config.guild(context.guild).enabled.set(toggle)
        self._enabled_cache[context.guild.id] = toggle

        if toggle:
            message = _(
                "When the queue runs out I will keep playing tracks recommended by YouTube "
                "based on whatever played last."
            )
        else:
            message = _("I will stop playing recommended tracks when the queue runs out.")

        await context.send(
            embed=await self.pylav.construct_embed(description=message, messageable=context),
            ephemeral=True,
        )

    @command_ytradio.command(name="buffer")
    @commands.admin_or_permissions(manage_guild=True)
    async def command_ytradio_buffer(self, context: PyLavContext, size: int) -> None:
        """Set how many recommended tracks to queue at a time (1-10)."""
        if isinstance(context, discord.Interaction):
            context = await self.bot.get_context(context)
        if context.interaction and not context.interaction.response.is_done():
            await context.defer(ephemeral=True)

        if not 1 <= size <= 10:
            await context.send(
                embed=await self.pylav.construct_embed(
                    description=_("Pick a number between 1 and 10."), messageable=context
                ),
                ephemeral=True,
            )
            return

        await self._config.guild(context.guild).buffer.set(size)
        self._buffer_cache[context.guild.id] = size
        await context.send(
            embed=await self.pylav.construct_embed(
                description=_("I will queue {number} recommended tracks at a time.").format(number=size),
                messageable=context,
            ),
            ephemeral=True,
        )

    @command_ytradio.command(name="reset")
    @commands.admin_or_permissions(manage_guild=True)
    async def command_ytradio_reset(self, context: PyLavContext) -> None:
        """Forget which tracks the radio has already played here."""
        if isinstance(context, discord.Interaction):
            context = await self.bot.get_context(context)
        if context.interaction and not context.interaction.response.is_done():
            await context.defer(ephemeral=True)

        self._played.pop(context.guild.id, None)
        await context.send(
            embed=await self.pylav.construct_embed(
                description=_("Radio history cleared."), messageable=context
            ),
            ephemeral=True,
        )
