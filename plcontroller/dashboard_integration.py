from __future__ import annotations

import logging
import typing as t

import discord
from redbot.core import commands

from pylav.players.query.obj import Query

log = logging.getLogger("red.plcontroller.dashboard")


def dashboard_page(*args: t.Any, **kwargs: t.Any) -> t.Callable[[t.Any], t.Any]:
    def decorator(func: t.Callable) -> t.Callable[[t.Any], t.Any]:
        func.__dashboard_decorator_params__ = (args, kwargs)
        return func

    return decorator


def _fmt_ms(milliseconds: float | int | None) -> str:
    """Format a millisecond duration as H:MM:SS / M:SS."""
    if not milliseconds or milliseconds < 0:
        return "0:00"
    total_seconds = int(milliseconds // 1000)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


class DashboardIntegration:
    bot: t.Any

    @commands.Cog.listener()
    async def on_dashboard_cog_add(self, dashboard_cog: commands.Cog) -> None:
        log.info("Dashboard cog found, registering PLController as a third party.")
        dashboard_cog.rpc.third_parties_handler.add_third_party(self)

    # ---------- helpers ----------

    # Actions any guild member may perform. Everything else is staff-only.
    LISTENER_ACTIONS = frozenset(
        {
            "pause", "resume", "skip", "previous", "shuffle",
            "seek", "volume_up", "volume_down", "volume_set",
            "search", "play", "play_now",
            "fav_add", "fav_play", "fav_queue",
        }
    )

    async def _dash_is_staff(self, user: discord.User, member: discord.Member, guild: discord.Guild) -> bool:
        return (
            await self.bot.is_owner(user)
            or member.id == guild.owner_id
            or member.guild_permissions.administrator
            or await self.bot.is_admin(member)
            or await self.bot.is_mod(member)
        )

    async def _dash_check_perms(
        self, user: discord.User, guild: discord.Guild
    ) -> tuple[discord.Member | None, dict | None]:
        """Any member of the guild may open the page; per-action gating happens later."""
        member = guild.get_member(user.id)
        if member is None:
            return None, {
                "status": 1,
                "error_title": "Member not found",
                "error_message": "You are not a member of this guild.",
            }
        return member, None

    def _dash_player(self, guild: discord.Guild):
        return self.pylav.get_player(guild)

    # ---------- pages ----------

    @dashboard_page(
        name=None,
        description="Control the music player from the web.",
        methods=("GET", "POST"),
    )
    async def dashboard_player_page(
        self, user: discord.User, guild: discord.Guild, **kwargs: t.Any
    ) -> dict[str, t.Any]:
        member, error = await self._dash_check_perms(user, guild)
        if error is not None:
            return error

        player = self._dash_player(guild)

        # --- handle an action, if one was submitted ---
        # The webserver builds this with `request.form.to_dict(flat=False)`, so every
        # value arrives as a list (e.g. {"action": ["pause"]}). Unwrap before comparing.
        raw_form = (kwargs.get("data") or {}).get("form") or {}

        def field(key: str, default=None):
            value = raw_form.get(key, default)
            if isinstance(value, (list, tuple)):
                return value[0] if value else default
            return value

        action = field("action") if kwargs.get("method") == "POST" else None

        # --- search: doesn't need an existing player ---
        if action == "search":
            search_term = (field("query") or "").strip()
            if not search_term:
                return {
                    "status": 0,
                    "notifications": [{"message": "Enter something to search for.", "category": "warning"}],
                }
            results, error = await self._dash_search(search_term)
            if error:
                return {"status": 0, "notifications": [{"message": error, "category": "danger"}]}
            return {
                "status": 0,
                "web_content": {
                    "source": PLAYER_TEMPLATE,
                    "player_state": await self._dash_build_state(player),
                    "search_results": results,
                    "search_term": search_term,
                    "favourites": await self._dash_fav_list(guild),
                    "is_staff": await self._dash_is_staff(user, member, guild),
                    "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                },
            }

        # --- guild favourites playlist ---
        if action in ("fav_add", "fav_remove", "fav_play", "fav_queue", "fav_clear"):
            message, category = await self._dash_favourites(action, member, guild, player, field)
            return {
                "status": 0,
                "notifications": [{"message": message, "category": category}],
                "redirect_url": kwargs.get("request_url"),
            }

        # --- play / enqueue: connects if needed ---
        if action in ("play", "play_now"):
            identifier = (field("identifier") or field("query") or "").strip()
            if not identifier:
                return {
                    "status": 0,
                    "notifications": [{"message": "Nothing to play.", "category": "warning"}],
                }
            message, category = await self._dash_play(
                member, guild, player, identifier, play_now=(action == "play_now")
            )
            return {
                "status": 0,
                "notifications": [{"message": message, "category": category}],
                "redirect_url": kwargs.get("request_url"),
            }

        if action and action not in self.LISTENER_ACTIONS:
            if not await self._dash_is_staff(user, member, guild):
                return {
                    "status": 0,
                    "notifications": [
                        {
                            "message": "Only moderators can do that. You can still play, pause, skip and queue music.",
                            "category": "warning",
                        }
                    ],
                }

        if action:
            if player is None:
                return {
                    "status": 0,
                    "notifications": [
                        {"message": "I am not connected to a voice channel.", "category": "warning"}
                    ],
                    "redirect_url": kwargs.get("request_url"),
                }
            try:
                if action == "pause":
                    await player.set_pause(True, member)
                elif action == "resume":
                    await player.set_pause(False, member)
                elif action == "skip":
                    await player.skip(member)
                elif action == "previous":
                    await player.previous(member)
                elif action == "stop":
                    await player.stop(member)
                elif action == "shuffle":
                    await player.shuffle_queue(member.id)
                elif action == "disconnect":
                    await player.disconnect(requester=member)
                elif action == "volume_up":
                    await player.set_volume(min(player.volume + 5, 1000), member)
                elif action == "volume_down":
                    await player.set_volume(max(player.volume - 5, 0), member)
                elif action == "volume_set":
                    raw = field("volume")
                    await player.set_volume(max(0, min(int(raw), 1000)), member)
                elif action == "seek":
                    # form sends seconds; PyLav wants milliseconds
                    await player.seek(float(field("position") or 0) * 1000, member)
                elif action == "repeat_track":
                    # Repeat state lives in async config, not a plain attribute.
                    current = await player.config.fetch_repeat_current()
                    await player.set_repeat("current", not current, member)
                elif action == "repeat_queue":
                    current = await player.config.fetch_repeat_queue()
                    await player.set_repeat("queue", not current, member)
                elif action == "repeat_off":
                    await player.set_repeat("disable", False, member)
                elif action == "clear_queue":
                    player.queue.clear()
                elif action == "remove_track":
                    # popindex() is PlayerQueue's supported positional removal.
                    player.queue.popindex(int(field("index")))
                else:
                    return {
                        "status": 0,
                        "notifications": [
                            {"message": f"Unknown action: {action}", "category": "warning"}
                        ],
                    }
            except Exception as exc:  # noqa: BLE001 - surfaced to the user, not swallowed
                log.exception("Dashboard player action %r failed", action)
                return {
                    "status": 0,
                    "notifications": [{"message": f"Action failed: {exc}", "category": "danger"}],
                }
            return {
                "status": 0,
                "notifications": [{"message": "Done.", "category": "success"}],
                "redirect_url": kwargs.get("request_url"),
            }

        # --- build the view ---
        return {
            "status": 0,
            "web_content": {
                "source": PLAYER_TEMPLATE,
                "player_state": await self._dash_build_state(player),
                # kwargs["csrf_token"] is (raw, signed); the signed value goes in the form.
                "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                "search_results": [],
                "search_term": "",
                "favourites": await self._dash_fav_list(guild),
                "is_staff": await self._dash_is_staff(user, member, guild),
            },
        }

    async def _dash_track_dict(self, track, position: int | None = None) -> dict[str, t.Any]:
        """PyLav track fields are async methods, so each one must be awaited."""

        async def safe(coro_method, default):
            if coro_method is None:
                return default
            try:
                value = await coro_method()
            except Exception:  # noqa: BLE001 - a single bad field shouldn't kill the page
                return default
            return default if value is None else value

        data = {
            "title": await safe(getattr(track, "title", None), "Unknown title"),
            "author": await safe(getattr(track, "author", None), ""),
            "uri": await safe(getattr(track, "uri", None), ""),
            "duration": _fmt_ms(await safe(getattr(track, "duration", None), 0)),
        }
        if position is not None:
            data["position"] = position
        return data

    async def _dash_build_state(self, player) -> dict[str, t.Any]:
        if player is None:
            return {"connected": False}

        current = player.current
        try:
            raw_queue = list(player.queue.raw_queue)
        except Exception:  # noqa: BLE001 - queue internals vary by version
            raw_queue = []

        queue_items = []
        for index, track in enumerate(raw_queue[:25], start=1):
            queue_items.append(await self._dash_track_dict(track, position=index))

        try:
            position_ms = await player.position()
        except Exception:  # noqa: BLE001
            position_ms = 0

        state: dict[str, t.Any] = {
            "connected": True,
            "position_ms": int(position_ms or 0),
            "position": _fmt_ms(position_ms),
            "paused": bool(player.paused),
            "playing": bool(player.is_playing),
            "volume": int(player.volume),
            "channel": getattr(getattr(player, "channel", None), "name", ""),
            "queue_length": len(raw_queue),
            "queue": queue_items,
            "current": None,
        }

        if current is not None:
            current_data = await self._dash_track_dict(current)
            try:
                current_data["duration_ms"] = int(await current.duration() or 0)
            except Exception:  # noqa: BLE001
                current_data["duration_ms"] = 0
            try:
                current_data["artwork"] = await current.artworkUrl() or ""
            except Exception:  # noqa: BLE001
                current_data["artwork"] = ""
            try:
                current_data["stream"] = bool(await current.stream())
            except Exception:  # noqa: BLE001
                current_data["stream"] = False
            state["current"] = current_data
        return state


    # ---------- search / play helpers ----------

    async def _dash_search(self, search_term: str, limit: int = 10):
        """Returns (results, error_message). Results are plain dicts for the template."""
        try:
            # A bare string can resolve to a single track; prefixing forces the
            # node to return a search result set instead of one match.
            looks_like_url = search_term.startswith(("http://", "https://", "spotify:"))
            query = await Query.from_string(
                search_term if looks_like_url else f"ytsearch:{search_term}"
            )
            # fullsearch=True is required, otherwise PyLav returns only the first match.
            response = await self.pylav.search_query(query, fullsearch=True)
        except Exception as exc:  # noqa: BLE001
            log.exception("Dashboard search failed for %r", search_term)
            return [], f"Search failed: {exc}"

        if response is None:
            return [], "No response from the audio node."

        load_type = getattr(response, "loadType", None)
        data = getattr(response, "data", None)

        if load_type == "error":
            return [], f"Search error: {getattr(data, 'message', 'unknown error')}"
        if load_type == "empty" or data is None:
            return [], None

        if load_type == "search":
            tracks = list(data)
        elif load_type == "playlist":
            tracks = list(getattr(data, "tracks", []))
        elif load_type == "track":
            tracks = [data]
        else:
            tracks = []

        results = []
        for track in tracks[:limit]:
            info = getattr(track, "info", None)
            if info is None:
                continue
            results.append(
                {
                    "title": getattr(info, "title", None) or "Unknown title",
                    "author": getattr(info, "author", None) or "",
                    "duration": _fmt_ms(getattr(info, "length", 0)),
                    "uri": getattr(info, "uri", None) or "",
                    "identifier": getattr(info, "uri", None) or "",
                    "artwork": getattr(info, "artworkUrl", None) or "",
                    "stream": bool(getattr(info, "isStream", False)),
                }
            )
        return results, None

    async def _dash_play(self, member, guild, player, identifier: str, play_now: bool = False):
        """Enqueue (or immediately play) a query/URL. Returns (message, category)."""
        # Connect if we have no player yet - the requester must be in a voice channel.
        if player is None:
            voice_state = getattr(member, "voice", None)
            channel = getattr(voice_state, "channel", None)
            if channel is None:
                return ("Join a voice channel first, then try again.", "warning")
            try:
                player = await self.pylav.player_manager.create(channel=channel, requester=member)
            except Exception as exc:  # noqa: BLE001
                log.exception("Dashboard could not create a player")
                return (f"Could not connect: {exc}", "danger")

        try:
            query = await Query.from_string(identifier)
            response = await self.pylav.get_tracks(query, player=player)
        except Exception as exc:  # noqa: BLE001
            log.exception("Dashboard could not resolve %r", identifier)
            return (f"Could not resolve that: {exc}", "danger")

        load_type = getattr(response, "loadType", None)
        data = getattr(response, "data", None)

        if load_type == "error":
            return (f"Load error: {getattr(data, 'message', 'unknown error')}", "danger")
        if load_type == "empty" or data is None:
            return ("Nothing found for that query.", "warning")

        if load_type == "playlist":
            tracks = list(getattr(data, "tracks", []))
        elif load_type == "search":
            tracks = list(data)[:1]
        else:
            tracks = [data]

        if not tracks:
            return ("Nothing found for that query.", "warning")

        try:
            if play_now or not player.current:
                await player.play(tracks[0], query, member)
                extra = tracks[1:]
            else:
                extra = tracks
            for track in extra:
                await player.add(requester=member.id, track=track, query=query)
        except Exception as exc:  # noqa: BLE001
            log.exception("Dashboard playback failed")
            return (f"Playback failed: {exc}", "danger")

        if len(tracks) > 1:
            return (f"Added {len(tracks)} tracks to the queue.", "success")
        return ("Added to the queue." if not play_now else "Now playing.", "success")


    # ---------- guild favourites ----------

    FAV_PLAYLIST_NAME = "Dashboard Favourites"

    async def _dash_get_fav_playlist(self, guild: discord.Guild, author_id: int):
        """Fetch (or create) the per-guild favourites playlist."""
        # Guild-scoped playlists use the guild id as both identifier and scope.
        return await self.pylav.playlist_db_manager.create_or_update_guild_playlist(
            guild=guild, author=author_id, name=self.FAV_PLAYLIST_NAME
        )

    async def _dash_favourites(self, action, member, guild, player, field):
        try:
            playlist = await self._dash_get_fav_playlist(guild, member.id)
        except Exception as exc:  # noqa: BLE001
            log.exception("Could not open the guild favourites playlist")
            return (f"Could not open the playlist: {exc}", "danger")

        try:
            if action == "fav_add":
                identifier = (field("identifier") or "").strip()
                if not identifier and player is not None and player.current is not None:
                    identifier = await player.current.uri()
                if not identifier:
                    return ("Nothing to save.", "warning")
                await playlist.add_track([identifier])
                return ("Saved to the guild favourites.", "success")

            if action == "fav_remove":
                identifier = (field("identifier") or "").strip()
                if not identifier:
                    return ("Nothing to remove.", "warning")
                await playlist.remove_track(identifier)
                return ("Removed from the guild favourites.", "success")

            if action == "fav_clear":
                await playlist.remove_all_tracks()
                return ("Cleared the guild favourites.", "success")

            # fav_play / fav_queue
            tracks = await playlist.fetch_tracks()
            if not tracks:
                return ("The guild favourites playlist is empty.", "warning")
            play_now = action == "fav_play"
            added = 0
            for entry in tracks:
                identifier = entry if isinstance(entry, str) else (entry or {}).get("encoded")
                if not identifier:
                    continue
                message, category = await self._dash_play(
                    member, guild, player, identifier, play_now=(play_now and added == 0)
                )
                if category == "danger":
                    return (message, category)
                player = self._dash_player(guild) or player
                added += 1
            return (f"Queued {added} track(s) from the guild favourites.", "success")
        except Exception as exc:  # noqa: BLE001
            log.exception("Favourites action %r failed", action)
            return (f"Favourites action failed: {exc}", "danger")

    async def _dash_fav_list(self, guild: discord.Guild):
        """Read-only listing of the favourites playlist for rendering."""
        try:
            playlist = await self.pylav.playlist_db_manager.create_or_update_guild_playlist(
                guild=guild, author=self.bot.user.id, name=self.FAV_PLAYLIST_NAME
            )
            raw = await playlist.fetch_tracks()
        except Exception:  # noqa: BLE001
            log.exception("Could not read the guild favourites playlist")
            return []
        out = []
        for entry in raw[:50]:
            identifier = entry if isinstance(entry, str) else (entry or {}).get("encoded")
            if identifier:
                out.append({"identifier": identifier})
        return out


PLAYER_TEMPLATE = """
<style>
  .plc { display:flex; flex-direction:column; gap:18px; }

  /* ---------- now playing ---------- */
  .plc-now {
    position:relative; overflow:hidden;
    display:flex; gap:18px; align-items:center; flex-wrap:wrap;
    padding:20px; border-radius:16px;
    background:rgba(24,48,105,.22); border:1px solid rgba(130,175,255,.16);
  }
  .plc-now-bg {
    position:absolute; inset:0; background-size:cover; background-position:center;
    filter:blur(28px) saturate(140%); opacity:.35; transform:scale(1.15); z-index:0;
  }
  .plc-now > * { position:relative; z-index:1; }
  .plc-art { height:112px; width:112px; border-radius:12px; object-fit:cover;
             box-shadow:0 10px 30px rgba(0,0,0,.55); flex:0 0 auto; }
  .plc-art-ph { background:rgba(255,255,255,.06); }
  .plc-meta { flex:1 1 260px; min-width:0; }
  .plc-title { font-size:1.15rem; font-weight:800; margin:0 0 3px; overflow:hidden;
               text-overflow:ellipsis; white-space:nowrap; }
  .plc-author { opacity:.72; font-size:.9rem; margin:0; }
  .plc-badges { margin-top:9px; display:flex; gap:6px; flex-wrap:wrap; }
  .plc-badge { font-size:.7rem; padding:3px 9px; border-radius:999px; font-weight:700;
               letter-spacing:.03em; text-transform:uppercase;
               background:rgba(255,255,255,.08); border:1px solid rgba(255,255,255,.12); }
  .plc-badge.live { background:rgba(237,66,69,.25); border-color:rgba(237,66,69,.5); }

  /* ---------- visualiser ---------- */
  .plc-viz { display:flex; align-items:flex-end; gap:3px; height:44px; flex:0 0 auto; }
  .plc-viz i {
    display:block; width:4px; border-radius:2px; background:linear-gradient(to top,#3ba55d,#5aa9ff);
    animation:plcBar 900ms ease-in-out infinite alternate;
  }
  .plc-viz.paused i { animation-play-state:paused; opacity:.35; }
  @keyframes plcBar { from { height:12%; } to { height:100%; } }

  /* ---------- seek ---------- */
  .plc-seek { display:flex; align-items:center; gap:12px; font-variant-numeric:tabular-nums; }
  .plc-seek input[type=range] { flex:1 1 auto; }
  .plc-time { font-size:.82rem; opacity:.75; min-width:44px; text-align:center; }

  /* ---------- controls ---------- */
  .plc-controls { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
  .plc-btn {
    display:inline-flex; align-items:center; justify-content:center; gap:7px;
    height:44px; min-width:44px; padding:0 15px; border-radius:11px; cursor:pointer;
    font-size:.88rem; font-weight:600; color:inherit; text-decoration:none;
    background:rgba(255,255,255,.05); border:1px solid rgba(255,255,255,.12);
    transition:background .15s ease, border-color .15s ease, transform .08s ease;
  }
  .plc-btn:hover { background:rgba(255,255,255,.12); }
  .plc-btn:active { transform:translateY(1px); }
  .plc-btn.round { border-radius:50%; padding:0; width:44px; }
  .plc-btn.play { width:56px; height:56px; border-radius:50%; font-size:1.15rem;
                  background:linear-gradient(135deg,#2f6fed,#5aa9ff); border-color:transparent; color:#fff; }
  .plc-btn.danger { border-color:rgba(255,90,90,.45); color:#ff8b8b; }
  .plc-btn.on { background:rgba(90,169,255,.22); border-color:rgba(90,169,255,.5); }

  /* ---------- panels / queue ---------- */
  .plc-panels { display:grid; gap:16px; grid-template-columns:1fr; }
  @media (min-width:1100px){ .plc-panels { grid-template-columns:3fr 2fr; } }
  .plc-panel { padding:16px; border-radius:14px;
               background:rgba(90,130,220,.06); border:1px solid rgba(120,160,255,.12); }
  .plc-panel h5 { margin:0 0 3px; font-size:.95rem; }
  .plc-hint { opacity:.6; font-size:.78rem; margin:0 0 11px; }
  .plc-row { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
  .plc-input { flex:1 1 240px; min-width:0; height:44px; padding:0 13px; border-radius:11px;
               background:rgba(0,0,0,.3); border:1px solid rgba(255,255,255,.12);
               color:inherit; font-size:.9rem; }
  .plc-input:focus { outline:none; border-color:rgba(130,175,255,.45); }
  .plc-q { width:100%; border-collapse:collapse; }
  .plc-q th, .plc-q td { text-align:left; padding:9px 10px; font-size:.86rem;
                         border-bottom:1px solid rgba(255,255,255,.06); }
  .plc-q th { opacity:.55; font-size:.7rem; text-transform:uppercase; letter-spacing:.05em; }
  .plc-q tr:last-child td { border-bottom:none; }
  .plc-thumb { width:42px; height:42px; border-radius:7px; object-fit:cover; }
  .plc-empty { opacity:.6; padding:22px; text-align:center; }
  .plc-sec-title { font-size:.72rem; text-transform:uppercase; letter-spacing:.06em;
                   font-weight:800; opacity:.55; margin:0 0 10px; }
</style>

{% if not player_state.connected %}
  <div class="plc-empty">
    <h4>{{ "Not connected" }}</h4>
    <p>Join a voice channel and play something below &mdash; I'll connect automatically.</p>
  </div>
{% else %}
<div class="plc">

  <div class="plc-now">
    {% if player_state.current and player_state.current.artwork %}
      <div class="plc-now-bg" style="background-image:url('{{ player_state.current.artwork }}');"></div>
      <img class="plc-art" src="{{ player_state.current.artwork }}" alt="" />
    {% else %}
      <div class="plc-art plc-art-ph"></div>
    {% endif %}

    <div class="plc-meta">
      {% if player_state.current %}
        <p class="plc-title" title="{{ player_state.current.title }}">{{ player_state.current.title }}</p>
        <p class="plc-author">{{ player_state.current.author }}</p>
        <div class="plc-badges">
          {% if player_state.current.stream %}<span class="plc-badge live">Live</span>{% endif %}
          {% if player_state.paused %}<span class="plc-badge">Paused</span>{% endif %}
          {% if player_state.channel %}<span class="plc-badge"><i class="fa fa-volume-up"></i> {{ player_state.channel }}</span>{% endif %}
          <span class="plc-badge"><i class="fa fa-list-ol"></i> {{ player_state.queue_length }} queued</span>
        </div>
      {% else %}
        <p class="plc-title">Nothing playing</p>
        <p class="plc-author">The queue is idle.</p>
      {% endif %}
    </div>

    <div class="plc-viz{% if player_state.paused or not player_state.current %} paused{% endif %}">
      {% for h in [40, 70, 100, 55, 85, 30, 65, 95, 45, 75, 35, 60] %}
        <i style="height:{{ h }}%; animation-duration:{{ 600 + h * 6 }}ms; animation-delay:{{ h * 4 }}ms;"></i>
      {% endfor %}
    </div>
  </div>

  {% if player_state.current and not player_state.current.stream and player_state.current.duration_ms %}
    <form method="POST" class="plc-seek">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <span class="plc-time">{{ player_state.position }}</span>
      <input type="range" name="position" min="0"
             max="{{ (player_state.current.duration_ms / 1000)|int }}"
             value="{{ (player_state.position_ms / 1000)|int }}"
             oninput="document.getElementById('plcSeekOut').textContent = this.value;" />
      <span class="plc-time">{{ player_state.current.duration }}</span>
      <button class="plc-btn" name="action" value="seek" title="Seek to position">
        <i class="fa fa-location-arrow"></i> Seek
      </button>
      <span class="plc-time" id="plcSeekOut" style="opacity:.45;"></span>
    </form>
  {% endif %}

  <form method="POST" class="plc-controls">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
    <button class="plc-btn round" name="action" value="previous" title="Previous"><i class="fa fa-step-backward"></i></button>
    {% if player_state.paused %}
      <button class="plc-btn play" name="action" value="resume" title="Resume"><i class="fa fa-play"></i></button>
    {% else %}
      <button class="plc-btn play" name="action" value="pause" title="Pause"><i class="fa fa-pause"></i></button>
    {% endif %}
    <button class="plc-btn round" name="action" value="skip" title="Skip"><i class="fa fa-step-forward"></i></button>
    <button class="plc-btn" name="action" value="shuffle" title="Shuffle the queue"><i class="fa fa-random"></i> Shuffle</button>
    {% if player_state.current %}
      <button class="plc-btn" name="action" value="fav_add" title="Save this track to the guild favourites"><i class="fa fa-star"></i> Favourite</button>
    {% endif %}
    <button class="plc-btn" name="action" value="repeat_track" title="Repeat current track"><i class="fa fa-repeat"></i> Track</button>
    <button class="plc-btn" name="action" value="repeat_queue" title="Repeat the queue"><i class="fa fa-refresh"></i> Queue</button>
    <button class="plc-btn" name="action" value="repeat_off" title="Turn repeat off"><i class="fa fa-ban"></i> Off</button>
    {% if is_staff %}
      <button class="plc-btn danger" name="action" value="stop" title="Stop and clear"><i class="fa fa-stop"></i></button>
      <button class="plc-btn danger" name="action" value="clear_queue" title="Empty the queue"><i class="fa fa-trash-o"></i> Queue</button>
      <button class="plc-btn danger" name="action" value="disconnect" title="Disconnect"><i class="fa fa-sign-out"></i></button>
    {% endif %}
  </form>

  <form method="POST" class="plc-seek">
    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
    <button class="plc-btn round" name="action" value="volume_down" title="Volume down"><i class="fa fa-volume-down"></i></button>
    <input type="range" name="volume" min="0" max="150" value="{{ player_state.volume }}"
           oninput="document.getElementById('plcVolOut').textContent = this.value + '%';" />
    <span class="plc-time" id="plcVolOut">{{ player_state.volume }}%</span>
    <button class="plc-btn" name="action" value="volume_set"><i class="fa fa-check"></i> Set</button>
    <button class="plc-btn round" name="action" value="volume_up" title="Volume up"><i class="fa fa-volume-up"></i></button>
  </form>

  <div>
    <p class="plc-sec-title">Queue &mdash; {{ player_state.queue_length }} track(s)</p>
    {% if player_state.queue %}
      <table class="plc-q">
        <thead><tr><th>#</th><th>Title</th><th>Channel</th><th>Length</th><th></th></tr></thead>
        <tbody>
          {% for item in player_state.queue %}
            <tr>
              <td style="opacity:.5;">{{ item.position }}</td>
              <td>{% if item.uri %}<a href="{{ item.uri }}" target="_blank">{{ item.title }}</a>{% else %}{{ item.title }}{% endif %}</td>
              <td style="opacity:.7;">{{ item.author }}</td>
              <td style="opacity:.7;">{{ item.duration }}</td>
              <td style="width:1%;">
                <form method="POST" style="display:inline;">
                  <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
                  <input type="hidden" name="index" value="{{ loop.index0 }}" />
                  <button class="plc-btn round danger" name="action" value="remove_track" title="Remove"><i class="fa fa-times"></i></button>
                </form>
              </td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
      {% if player_state.queue_length > 25 %}
        <p class="plc-empty">Showing the first 25 of {{ player_state.queue_length }} tracks.</p>
      {% endif %}
    {% else %}
      <p class="plc-empty">The queue is empty.</p>
    {% endif %}
  </div>

  <div class="plc-panels">
    <div class="plc-panel">
      <h5><i class="fa fa-search me-1"></i> Search &amp; play</h5>
      <p class="plc-hint">Searches YouTube and any other source your nodes support.</p>
      <form method="POST" class="plc-row">
        <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
        <input class="plc-input" type="text" name="query" placeholder="Song, artist, or a link..."
               value="{{ search_term or '' }}" />
        <button class="plc-btn" name="action" value="search"><i class="fa fa-search"></i> Search</button>
      </form>

      {% if search_results %}
        <table class="plc-q" style="margin-top:12px;">
          <tbody>
            {% for r in search_results %}
              <tr>
                <td style="width:54px;">
                  {% if r.artwork %}<img class="plc-thumb" src="{{ r.artwork }}" alt="" />{% endif %}
                </td>
                <td>
                  {% if r.uri %}<a href="{{ r.uri }}" target="_blank">{{ r.title }}</a>{% else %}{{ r.title }}{% endif %}
                  <div style="opacity:.6; font-size:.8rem;">{{ r.author }}</div>
                </td>
                <td style="opacity:.7; width:70px;">{% if r.stream %}Live{% else %}{{ r.duration }}{% endif %}</td>
                <td style="white-space:nowrap; width:1%;">
                  <form method="POST" style="display:inline;">
                    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
                    <input type="hidden" name="identifier" value="{{ r.identifier }}" />
                    <button class="plc-btn round" name="action" value="play" title="Add to queue"><i class="fa fa-plus"></i></button>
                  </form>
                  <form method="POST" style="display:inline;">
                    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
                    <input type="hidden" name="identifier" value="{{ r.identifier }}" />
                    <button class="plc-btn round play" style="width:44px;height:44px;" name="action" value="play_now" title="Play now"><i class="fa fa-play"></i></button>
                  </form>
                </td>
              </tr>
            {% endfor %}
          </tbody>
        </table>
      {% elif search_term %}
        <p class="plc-empty">No results for &ldquo;{{ search_term }}&rdquo;.</p>
      {% endif %}
    </div>

    <div class="plc-panel">
      <h5><i class="fa fa-rss me-1"></i> Radio / direct stream</h5>
      <p class="plc-hint">Icecast/Shoutcast, .mp3, .m3u8 and similar direct URLs.</p>
      <form method="POST" class="plc-row">
        <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
        <input class="plc-input" type="text" name="identifier" placeholder="https://stream.example.com/live.mp3" />
        <button class="plc-btn" name="action" value="play"><i class="fa fa-plus"></i> Queue</button>
        <button class="plc-btn play" style="width:auto;height:44px;border-radius:11px;padding:0 15px;" name="action" value="play_now"><i class="fa fa-play"></i> Play</button>
      </form>
    </div>
  </div>

  <div class="plc-panel">
    <div style="display:flex; align-items:center; justify-content:space-between; gap:10px; flex-wrap:wrap;">
      <div>
        <h5><i class="fa fa-star me-1"></i> Guild favourites</h5>
        <p class="plc-hint">Shared playlist for this server &mdash; any member can add to it.</p>
      </div>
      <form method="POST" class="plc-row">
        <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
        <button class="plc-btn" name="action" value="fav_queue" title="Queue every favourite"><i class="fa fa-plus"></i> Queue all</button>
        <button class="plc-btn play" style="width:auto;height:44px;border-radius:11px;padding:0 15px;" name="action" value="fav_play" title="Play the favourites now"><i class="fa fa-play"></i> Play all</button>
        {% if is_staff %}
          <button class="plc-btn danger" name="action" value="fav_clear" title="Remove every favourite"><i class="fa fa-trash-o"></i></button>
        {% endif %}
      </form>
    </div>

    {% if favourites %}
      <table class="plc-q" style="margin-top:10px;">
        <tbody>
          {% for fav in favourites %}
            <tr>
              <td style="opacity:.5; width:34px;">{{ loop.index }}</td>
              <td style="word-break:break-all; font-size:.82rem;">{{ fav.identifier }}</td>
              <td style="white-space:nowrap; width:1%;">
                <form method="POST" style="display:inline;">
                  <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
                  <input type="hidden" name="identifier" value="{{ fav.identifier }}" />
                  <button class="plc-btn round" name="action" value="play" title="Queue"><i class="fa fa-plus"></i></button>
                </form>
                {% if is_staff %}
                  <form method="POST" style="display:inline;">
                    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
                    <input type="hidden" name="identifier" value="{{ fav.identifier }}" />
                    <button class="plc-btn round danger" name="action" value="fav_remove" title="Remove"><i class="fa fa-times"></i></button>
                  </form>
                {% endif %}
              </td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
    {% else %}
      <p class="plc-empty">No favourites yet. Hit <b>Favourite</b> while a track is playing.</p>
    {% endif %}
  </div>

  {% if not is_staff %}
    <p class="plc-hint" style="text-align:center;">
      <i class="fa fa-info-circle"></i>
      You can play, pause, skip and queue music. Stopping and disconnecting are moderator-only.
    </p>
  {% endif %}

</div>
{% endif %}
"""
