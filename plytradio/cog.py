from __future__ import annotations

import asyncio
import contextlib
import inspect
import random
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

import discord
from redbot.core import Config, commands
from redbot.core.i18n import Translator, cog_i18n

from pylav import logging
from pylav.core.context import PyLavContext
from pylav.events.track import TrackEndEvent, TrackSkippedEvent, TrackStartEvent
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

# How many alternate seeds to try when a mix runs out of unheard tracks.
RESEED_ATTEMPTS = 4

# Top the queue up once it drops to this many tracks. Keeping it low means
# tracks the user queued themselves are left alone until they're nearly done.
LOW_WATER_MARK = 1


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
        # Video IDs we have already used as mix seeds, so reseeding keeps
        # branching outward instead of circling back to the same mixes.
        self._seeds: dict[int, deque[str]] = defaultdict(lambda: deque(maxlen=SEED_MEMORY))
        self._lock: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def cog_unload(self) -> None:
        self._played.clear()
        self._seeds.clear()

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

    @staticmethod
    def _extract_tracks(response: Any) -> list[Any]:
        """Pull the track list out of a loadtracks response.

        PyLav's response objects changed shape between Lavalink v3 and v4:

          v3: TrackLoaded / PlaylistLoaded / SearchResult, all with .tracks
          v4: TrackResponse / PlaylistResponse / SearchResponse, where the
              payload lives under .data -- a list for searches, an object
              with .tracks for playlists, a bare track for single loads.

        This handles both so the cog doesn't break on a PyLav update.
        """
        if not response:
            return []

        # v3 shape, and v4 responses that still expose a flat list.
        tracks = getattr(response, "tracks", None)
        if tracks:
            return list(tracks)

        data = getattr(response, "data", None)
        if data is None:
            return []
        if isinstance(data, list):
            return list(data)

        nested = getattr(data, "tracks", None)
        if nested:
            return list(nested)

        # Single track load: data is the track itself.
        if getattr(data, "encoded", None) or getattr(data, "info", None):
            return [data]
        return []

    @classmethod
    def _unique(cls, tracks: list[Any]) -> list[Any]:
        """Drop duplicates while preserving order -- mixes can list a song twice."""
        seen: set[str] = set()
        out = []
        for track in tracks:
            key = cls._track_key(track)
            if key is None or key in seen:
                continue
            seen.add(key)
            out.append(track)
        return out

    @staticmethod
    def _track_key(track: Any) -> str | None:
        """Stable identity for a track, used for de-duplication.

        The video identifier is far more reliable than the encoded blob:
        encoded strings carry position and version data, so the same song
        fetched twice can produce two different strings and slip past a
        de-dupe keyed on them.
        """
        info = getattr(track, "info", None)
        identifier = getattr(info, "identifier", None)
        if identifier:
            return identifier
        with contextlib.suppress(Exception):
            if encoded := getattr(track, "encoded", None):
                return encoded
        return None

    @staticmethod
    async def _resolve(value):
        """Return a value whether the accessor was sync or async.

        PyLav has moved some Track accessors between coroutines and plain
        properties across versions. Awaiting a plain string raises, and that
        exception was being swallowed -- leaving the key empty, which silently
        disabled de-duplication and let the playing track be queued again.
        """
        if inspect.isawaitable(value):
            return await value
        return value

    async def _seed_key(self, track: Track) -> str | None:
        """Stable identity for a live player track."""
        for accessor in ("identifier", "encoded"):
            try:
                attribute = getattr(track, accessor, None)
                if attribute is None:
                    continue
                value = attribute() if callable(attribute) else attribute
                if resolved := await self._resolve(value):
                    return resolved
            except Exception:  # noqa: BLE001 - try the next accessor
                continue
        return None

    async def _youtube_id_for(self, track: Track) -> str | None:
        """Return a YouTube video ID to seed the mix from.

        YouTube tracks hand us their identifier directly. Anything else
        (Spotify, Deezer, Apple Music) has no video ID, so we resolve it by
        searching YouTube for the title and artist and taking the top hit.
        """
        with contextlib.suppress(Exception):
            source = await self._resolve(track.source() if callable(track.source) else track.source)
            identifier = await self._seed_key(track)
            if source and "youtube" in source.lower() and identifier:
                return identifier

        try:
            title = await self._resolve(track.title() if callable(track.title) else track.title)
            author = await self._resolve(track.author() if callable(track.author) else track.author)
        except Exception:
            LOGGER.debug("Could not read metadata off the finished track")
            return None

        terms = " ".join(part for part in (title, author) if part).strip()
        if not terms:
            return None

        query = await Query.from_string(f"ytsearch:{terms}")
        # get_tracks drops search results unless fullsearch is True --
        # the branch is `fullsearch and is_search or is_single`, and a
        # search query is not is_single, so False here returns nothing.
        response = await self.pylav.get_tracks(query, fullsearch=True)
        results = self._extract_tracks(response)
        if not results:
            LOGGER.debug("No YouTube match found for %s", terms)
            return None

        candidate = await Track.build_track(
            node=await self.pylav.node_manager.find_best_node(),
            data=results[0],
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
        tracks = self._extract_tracks(response)
        if not tracks:
            LOGGER.debug(
                "Mix RD%s returned no usable tracks (response type: %s)",
                video_id,
                type(response).__name__,
            )
        return tracks

    # ------------------------------------------------------------------
    # Shared top-up logic
    # ------------------------------------------------------------------

    async def _top_up(self, player: Player, seed_track: Track | None, start_playback: bool) -> int:
        """Fetch radio tracks seeded from ``seed_track`` and queue them.

        Returns the number of tracks added.
        """
        guild_id = player.guild.id
        if seed_track is None:
            LOGGER.debug("Guild %s: no seed track available", guild_id)
            return 0
        if not player.is_connected:
            LOGGER.debug("Guild %s: player disconnected", guild_id)
            return 0

        with contextlib.suppress(Exception):
            if key := await self._seed_key(seed_track):
                self._played[guild_id].append(key)

        video_id = await self._youtube_id_for(seed_track)
        if not video_id:
            LOGGER.debug("Guild %s: could not resolve a YouTube seed", guild_id)
            return 0

        candidates = await self._fetch_mix(video_id, player)
        if not candidates:
            return 0

        played = set(self._played[guild_id])
        # Also exclude anything sitting in the queue right now, otherwise a
        # mix that lists the same song twice can queue it twice.
        # These are live pylav Track objects whose identifier() is async, so
        # they need awaiting -- comparing raw encoded blobs against the
        # identifiers used everywhere else silently matches nothing.
        queued: set[str] = set()
        with contextlib.suppress(Exception):
            for entry in list(player.queue.raw_queue):
                if key := await self._seed_key(entry):
                    queued.add(key)
        if player.current is not None:
            with contextlib.suppress(Exception):
                if key := await self._seed_key(player.current):
                    queued.add(key)

        excluded = played | queued
        fresh = self._unique([t for t in candidates if self._track_key(t) not in excluded])

        # A YouTube mix is a fixed pool of roughly 25 tracks, and seeding it
        # from a song that is itself in that mix returns much the same list.
        # Once we have heard all of them, branch onto a different mix rather
        # than replaying what just went by.
        if not fresh:
            LOGGER.debug("Guild %s: mix RD%s exhausted, reseeding", guild_id, video_id)
            fresh, candidates = await self._reseed(player, candidates, excluded)

        pool = fresh or self._unique(candidates)
        if not fresh:
            LOGGER.debug("Guild %s: no fresh tracks anywhere, allowing repeats", guild_id)

        target = max(1, await self.get_buffer(guild_id))
        # Never force a minimum of one here. With buffer=1 the queue already
        # holds its target after a single top-up, and max(1, ...) would add
        # another track anyway on every track start -- that is the doubling.
        shortfall = target - player.queue.qsize()
        if shortfall <= 0:
            LOGGER.debug("Guild %s: queue already at target (%s)", guild_id, target)
            return 0
        # Random rather than the first N: taking the head of the list every
        # time makes the radio walk the same few tracks in the same order.
        chosen = random.sample(pool, min(shortfall, len(pool)))
        if not chosen:
            return 0

        for track in chosen:
            if key := self._track_key(track):
                self._played[guild_id].append(key)

        requester = player.guild.me
        await player.bulk_add(tracks_and_queries=chosen, requester=requester.id)

        if start_playback and player.current is None:
            await player.play(None, None, requester=requester)

        LOGGER.debug("Guild %s: queued %s radio tracks", guild_id, len(chosen))
        return len(chosen)

    async def _reseed(self, player: Player, candidates: list[Any], played: set[str]) -> tuple[list[Any], list[Any]]:
        """Branch onto a different YouTube mix when the current one is used up.

        Picks a track from the exhausted mix that we have not already seeded
        from and fetches its mix instead. Returns (fresh, candidates).
        """
        guild_id = player.guild.id
        seeds_tried = self._seeds[guild_id]

        options = [t for t in candidates if (k := self._track_key(t)) and k not in seeds_tried]
        random.shuffle(options)

        for alternate in options[:RESEED_ATTEMPTS]:
            key = self._track_key(alternate)
            seeds_tried.append(key)
            more = await self._fetch_mix(key, player)
            if not more:
                continue
            fresh = [t for t in more if self._track_key(t) not in played]
            if fresh:
                LOGGER.debug("Guild %s: reseeded onto mix RD%s (%s fresh)", guild_id, key, len(fresh))
                return fresh, more

        return [], candidates

    # ------------------------------------------------------------------
    # Hooks
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_pylav_track_start_event(self, event: TrackStartEvent) -> None:
        """Keep the queue topped up so it never actually runs dry.

        This is what makes skip work. If we only reacted to the queue
        emptying, skipping the last track would call next() on an empty
        queue, which stops the player and reports the end reason as
        STOPPED -- indistinguishable from the user pressing stop.
        """
        player: Player = event.player
        if player is None or player.guild is None:
            return
        guild_id = player.guild.id

        if not await self.is_enabled(guild_id):
            return
        # Only step in when the queue is nearly exhausted, so tracks the
        # user queued themselves play through untouched.
        if player.queue.qsize() > LOW_WATER_MARK:
            return
        if self._lock[guild_id].locked():
            return

        async with self._lock[guild_id]:
            if player.queue.qsize() > LOW_WATER_MARK:
                return
            await self._top_up(player, player.current or event.track, start_playback=False)

    @commands.Cog.listener()
    async def on_pylav_track_skipped_event(self, event: TrackSkippedEvent) -> None:
        """Safety net: a skip that emptied the player should not end the session."""
        player: Player = event.player
        if player is None or player.guild is None:
            return
        guild_id = player.guild.id

        if not await self.is_enabled(guild_id):
            return

        async with self._lock[guild_id]:
            if player.current is not None or not player.queue.empty():
                return
            LOGGER.debug("Guild %s: skip emptied the player, reloading radio", guild_id)
            await self._top_up(player, event.track, start_playback=True)

    @commands.Cog.listener()
    async def on_pylav_track_end_event(self, event: TrackEndEvent) -> None:
        """Fallback for when a track finishes and nothing is left to play."""
        player: Player = event.player
        if player is None or player.guild is None:
            return
        guild_id = player.guild.id

        if event.reason not in NATURAL_END_REASONS:
            LOGGER.debug("Track ended in guild %s with reason %s - ignoring", guild_id, event.reason)
            return
        if not await self.is_enabled(guild_id):
            return

        async with self._lock[guild_id]:
            # PyLav has already run next() by the time this fires. If
            # something is playing or queued, we should stay out of the way.
            if player.current is not None or not player.queue.empty():
                return
            LOGGER.debug("Guild %s: queue ran dry, reloading radio", guild_id)
            await self._top_up(player, event.track, start_playback=True)

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

    @command_ytradio.command(name="diagnose", aliases=["test"])
    @commands.admin_or_permissions(manage_guild=True)
    async def command_ytradio_diagnose(self, context: PyLavContext) -> None:
        """Walk the radio pipeline against the current track and report each step."""
        if isinstance(context, discord.Interaction):
            context = await self.bot.get_context(context)
        if context.interaction and not context.interaction.response.is_done():
            await context.defer(ephemeral=True)

        lines: list[str] = []
        guild_id = context.guild.id

        enabled = await self.is_enabled(guild_id)
        lines.append(f"{'PASS' if enabled else 'FAIL'} - radio enabled: {enabled}")

        player: Player | None = self.pylav.get_player(guild_id)
        if player is None:
            lines.append("FAIL - no player. Play something first, then run this.")
            await self._send_diagnosis(context, lines)
            return
        lines.append("PASS - player exists")

        builtin = False
        with contextlib.suppress(Exception):
            builtin = await player.autoplay_enabled()
        if builtin:
            lines.append("FAIL - PyLav autoplay is ON and will pre-empt the radio.")
            lines.append("       Run: [p]playerset server auto false")
        else:
            lines.append("PASS - PyLav built-in autoplay is off")

        with contextlib.suppress(Exception):
            dc = await player.config.fetch_empty_queue_dc()
            if getattr(dc, "enabled", False):
                lines.append("WARN - empty-queue disconnect is on; bot may leave before the radio loads.")

        seed = player.current or player.last_track
        if seed is None:
            lines.append("FAIL - nothing playing and no last track to seed from.")
            await self._send_diagnosis(context, lines)
            return

        with contextlib.suppress(Exception):
            lines.append(f"PASS - seed track: {await seed.title()}")

        with contextlib.suppress(Exception):
            if await seed.stream():
                lines.append("WARN - seed is a livestream. Streams never end, so the")
                lines.append("       radio hook will never fire while one is playing.")

        video_id = await self._youtube_id_for(seed)
        if not video_id:
            lines.append("FAIL - could not resolve a YouTube video ID for the seed.")
            await self._send_diagnosis(context, lines)
            return
        lines.append(f"PASS - resolved video ID: {video_id}")

        tracks = await self._fetch_mix(video_id, player)
        if not tracks:
            lines.append(f"FAIL - mix RD{video_id} returned no tracks.")
            lines.append("       Your Lavalink node's YouTube source may be broken,")
            lines.append("       or this video has no mix available.")
            await self._send_diagnosis(context, lines)
            return
        lines.append(f"PASS - mix returned {len(tracks)} tracks")

        already = len(self._played[guild_id])
        lines.append(f"INFO - {already} tracks in this guild's radio memory")
        lines.append("")
        lines.append("Pipeline works. If playback still stops, the listener isn't")
        lines.append("firing - check that track end reason is FINISHED, not STOPPED.")

        await self._send_diagnosis(context, lines)

    async def _send_diagnosis(self, context: PyLavContext, lines: list[str]) -> None:
        body = "\n".join(lines)
        await context.send(
            embed=await self.pylav.construct_embed(
                title=_("YouTube radio diagnostics"),
                description=f"```\n{body}\n```",
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
        self._seeds.pop(context.guild.id, None)
        await context.send(
            embed=await self.pylav.construct_embed(
                description=_("Radio history cleared."), messageable=context
            ),
            ephemeral=True,
        )
