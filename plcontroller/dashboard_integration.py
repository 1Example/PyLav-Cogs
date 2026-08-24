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

    async def _dash_check_perms(
        self, user: discord.User, guild: discord.Guild
    ) -> tuple[discord.Member | None, dict | None]:
        """Returns (member, error_payload). error_payload is None when allowed."""
        member = guild.get_member(user.id)
        if member is None:
            return None, {
                "status": 1,
                "error_title": "Member not found",
                "error_message": "You are not a member of this guild.",
            }
        allowed = (
            await self.bot.is_owner(user)
            or member.id == guild.owner_id
            or member.guild_permissions.administrator
            or await self.bot.is_admin(member)
            or await self.bot.is_mod(member)
        )
        if not allowed:
            return None, {
                "status": 1,
                "error_title": "Insufficient permissions",
                "error_message": "You need to be a moderator to control the player.",
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
                    "csrf_token_value": (kwargs.get("csrf_token") or ("", ""))[1],
                },
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

        state: dict[str, t.Any] = {
            "connected": True,
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
            query = await Query.from_string(search_term)
            response = await self.pylav.search_query(query)
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


PLAYER_TEMPLATE = """
<style>
  .plc-wrap { display: flex; flex-direction: column; gap: 16px; }
  .plc-now {
    display: flex; gap: 16px; align-items: center; flex-wrap: wrap;
    padding: 16px; border-radius: 12px;
    background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.08);
  }
  .plc-art {
    height: 96px; width: 96px; border-radius: 10px; object-fit: cover;
    background: rgba(255,255,255,0.06); flex: 0 0 auto;
  }
  .plc-meta { flex: 1 1 240px; min-width: 0; }
  .plc-title {
    font-size: 1.1rem; font-weight: 700; margin: 0 0 2px 0;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .plc-author { opacity: 0.7; font-size: 0.88rem; margin: 0; }
  .plc-badges { margin-top: 8px; display: flex; gap: 6px; flex-wrap: wrap; }
  .plc-badge {
    font-size: 0.72rem; padding: 2px 8px; border-radius: 999px;
    background: rgba(255,255,255,0.07); border: 1px solid rgba(255,255,255,0.1);
  }
  .plc-controls { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  .plc-btn {
    display: inline-flex; align-items: center; justify-content: center;
    height: 42px; min-width: 42px; padding: 0 14px; gap: 6px;
    border-radius: 10px; cursor: pointer; font-size: 0.9rem; font-weight: 600;
    background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.12);
    color: inherit; text-decoration: none;
  }
  .plc-btn:hover { background: rgba(255,255,255,0.11); }
  .plc-btn.primary { background: #2f6fed; border-color: #2f6fed; color: #fff; }
  .plc-btn.danger  { border-color: rgba(255,90,90,0.5); color: #ff7b7b; }
  .plc-vol { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .plc-vol input[type=range] { width: 180px; }
  .plc-queue { width: 100%; border-collapse: collapse; }
  .plc-queue th, .plc-queue td {
    text-align: left; padding: 8px 10px; font-size: 0.86rem;
    border-bottom: 1px solid rgba(255,255,255,0.06);
  }
  .plc-queue th { opacity: 0.6; font-size: 0.74rem; text-transform: uppercase; }
  .plc-empty { opacity: 0.65; padding: 24px; text-align: center; }
  .plc-panels { display: grid; gap: 16px; grid-template-columns: 1fr; margin-top: 4px; }
  @media (min-width: 1100px) { .plc-panels { grid-template-columns: 3fr 2fr; } }
  .plc-panel {
    padding: 16px; border-radius: 12px;
    background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.08);
  }
  .plc-panel h5 { margin: 0 0 4px 0; font-size: 1rem; }
  .plc-hint { opacity: 0.6; font-size: 0.8rem; margin: 0 0 10px 0; }
  .plc-row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  .plc-label { font-size: 0.78rem; opacity: 0.7; }
  .plc-input {
    flex: 1 1 240px; min-width: 0; height: 42px; padding: 0 12px;
    border-radius: 10px; background: rgba(0,0,0,0.25);
    border: 1px solid rgba(255,255,255,0.12); color: inherit; font-size: 0.9rem;
  }
  .plc-input:focus { outline: none; border-color: rgba(255,255,255,0.3); }
</style>

{% if not player_state.connected %}
  <div class="plc-empty">
    <h4>Not connected</h4>
    <p>Join a voice channel and play something below - I'll connect automatically.</p>
  </div>

  <div class="plc-panels">

    <div class="plc-panel">
      <h5>Search &amp; play</h5>
      <p class="plc-hint">Search YouTube (and every other source your nodes support), then queue a result.</p>
      <form method="POST" class="plc-row">
        <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
        <input class="plc-input" type="text" name="query" placeholder="Search for a song, artist, or paste a link..."
               value="{{ search_term or '' }}" />
        <button class="plc-btn primary" name="action" value="search">Search</button>
      </form>

      {% if search_results %}
        <table class="plc-queue" style="margin-top:12px;">
          <thead><tr><th></th><th>Title</th><th>Artist</th><th>Length</th><th></th></tr></thead>
          <tbody>
            {% for r in search_results %}
              <tr>
                <td style="width:52px;">
                  {% if r.artwork %}<img src="{{ r.artwork }}" alt="" style="width:44px;height:44px;border-radius:6px;object-fit:cover;" />{% endif %}
                </td>
                <td>{% if r.uri %}<a href="{{ r.uri }}" target="_blank">{{ r.title }}</a>{% else %}{{ r.title }}{% endif %}</td>
                <td>{{ r.author }}</td>
                <td>{% if r.stream %}LIVE{% else %}{{ r.duration }}{% endif %}</td>
                <td style="white-space:nowrap;">
                  <form method="POST" style="display:inline;">
                    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
                    <input type="hidden" name="identifier" value="{{ r.identifier }}" />
                    <button class="plc-btn" name="action" value="play" title="Add to queue">+ Queue</button>
                  </form>
                  <form method="POST" style="display:inline;">
                    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
                    <input type="hidden" name="identifier" value="{{ r.identifier }}" />
                    <button class="plc-btn primary" name="action" value="play_now" title="Play immediately">&#9654;</button>
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
      <h5>Radio / direct stream</h5>
      <p class="plc-hint">Paste a direct stream or radio URL (Icecast/Shoutcast, .mp3, .m3u8, and so on).</p>
      <form method="POST" class="plc-row">
        <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
        <input class="plc-input" type="text" name="identifier" placeholder="https://stream.example.com/live.mp3" />
        <button class="plc-btn" name="action" value="play">Queue</button>
        <button class="plc-btn primary" name="action" value="play_now">Play now</button>
      </form>
    </div>

  </div>
{% else %}
  <div class="plc-wrap">

    <div class="plc-now">
      {% if player_state.current and player_state.current.artwork %}
        <img class="plc-art" src="{{ player_state.current.artwork }}" alt="" />
      {% else %}
        <div class="plc-art"></div>
      {% endif %}
      <div class="plc-meta">
        {% if player_state.current %}
          <p class="plc-title">{{ player_state.current.title }}</p>
          <p class="plc-author">{{ player_state.current.author }}</p>
          <div class="plc-badges">
            <span class="plc-badge">{{ player_state.current.duration }}</span>
            {% if player_state.current.stream %}<span class="plc-badge">LIVE</span>{% endif %}
            {% if player_state.paused %}<span class="plc-badge">Paused</span>{% endif %}
            {% if player_state.channel %}<span class="plc-badge">{{ player_state.channel }}</span>{% endif %}
          </div>
        {% else %}
          <p class="plc-title">Nothing playing</p>
          <p class="plc-author">The queue is idle.</p>
        {% endif %}
      </div>
    </div>

    <form method="POST" class="plc-controls">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <button class="plc-btn" name="action" value="previous" title="Previous">&#9198;</button>
      {% if player_state.paused %}
        <button class="plc-btn primary" name="action" value="resume" title="Resume">&#9654; Resume</button>
      {% else %}
        <button class="plc-btn primary" name="action" value="pause" title="Pause">&#10073;&#10073; Pause</button>
      {% endif %}
      <button class="plc-btn" name="action" value="skip" title="Skip">&#9197;</button>
      <button class="plc-btn" name="action" value="shuffle" title="Shuffle queue">&#128256; Shuffle</button>
      <button class="plc-btn danger" name="action" value="stop" title="Stop and clear queue">&#9632; Stop</button>
      <button class="plc-btn danger" name="action" value="disconnect" title="Disconnect">Disconnect</button>
    </form>

    <form method="POST" class="plc-controls">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <button class="plc-btn" name="action" value="repeat_track" title="Repeat current track">&#128257; Repeat track</button>
      <button class="plc-btn" name="action" value="repeat_queue" title="Repeat queue">&#128256; Repeat queue</button>
      <button class="plc-btn" name="action" value="repeat_off" title="Turn repeat off">Repeat off</button>
      <button class="plc-btn danger" name="action" value="clear_queue" title="Empty the queue">Clear queue</button>
    </form>

    <form method="POST" class="plc-vol">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <label class="plc-label" for="plcSeek">Seek to (seconds)</label>
      <input class="plc-input" style="max-width:120px;" id="plcSeek" type="number" name="position" min="0" step="1" value="0" />
      <button class="plc-btn" name="action" value="seek">Go</button>
    </form>

    <form method="POST" class="plc-vol">
      <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
      <button class="plc-btn" name="action" value="volume_down" title="Volume down">&#8722;</button>
      <input type="range" name="volume" min="0" max="150" value="{{ player_state.volume }}"
             oninput="document.getElementById('plcVolOut').textContent = this.value + '%';" />
      <span id="plcVolOut">{{ player_state.volume }}%</span>
      <button class="plc-btn" name="action" value="volume_set">Set</button>
      <button class="plc-btn" name="action" value="volume_up" title="Volume up">+</button>
    </form>

    <div>
      <h5>Queue &mdash; {{ player_state.queue_length }} track(s)</h5>
      {% if player_state.queue %}
        <table class="plc-queue">
          <thead>
            <tr><th>#</th><th>Title</th><th>Artist</th><th>Length</th><th></th></tr>
          </thead>
          <tbody>
            {% for item in player_state.queue %}
              <tr>
                <td>{{ item.position }}</td>
                <td>{% if item.uri %}<a href="{{ item.uri }}" target="_blank">{{ item.title }}</a>{% else %}{{ item.title }}{% endif %}</td>
                <td>{{ item.author }}</td>
                <td>{{ item.duration }}</td>
                <td style="width:1%;white-space:nowrap;">
                  <form method="POST" style="display:inline;">
                    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
                    <input type="hidden" name="index" value="{{ loop.index0 }}" />
                    <button class="plc-btn danger" name="action" value="remove_track" title="Remove">&times;</button>
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
      <h5>Search &amp; play</h5>
      <p class="plc-hint">Search YouTube (and every other source your nodes support), then queue a result.</p>
      <form method="POST" class="plc-row">
        <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
        <input class="plc-input" type="text" name="query" placeholder="Search for a song, artist, or paste a link..."
               value="{{ search_term or '' }}" />
        <button class="plc-btn primary" name="action" value="search">Search</button>
      </form>

      {% if search_results %}
        <table class="plc-queue" style="margin-top:12px;">
          <thead><tr><th></th><th>Title</th><th>Artist</th><th>Length</th><th></th></tr></thead>
          <tbody>
            {% for r in search_results %}
              <tr>
                <td style="width:52px;">
                  {% if r.artwork %}<img src="{{ r.artwork }}" alt="" style="width:44px;height:44px;border-radius:6px;object-fit:cover;" />{% endif %}
                </td>
                <td>{% if r.uri %}<a href="{{ r.uri }}" target="_blank">{{ r.title }}</a>{% else %}{{ r.title }}{% endif %}</td>
                <td>{{ r.author }}</td>
                <td>{% if r.stream %}LIVE{% else %}{{ r.duration }}{% endif %}</td>
                <td style="white-space:nowrap;">
                  <form method="POST" style="display:inline;">
                    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
                    <input type="hidden" name="identifier" value="{{ r.identifier }}" />
                    <button class="plc-btn" name="action" value="play" title="Add to queue">+ Queue</button>
                  </form>
                  <form method="POST" style="display:inline;">
                    <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
                    <input type="hidden" name="identifier" value="{{ r.identifier }}" />
                    <button class="plc-btn primary" name="action" value="play_now" title="Play immediately">&#9654;</button>
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
      <h5>Radio / direct stream</h5>
      <p class="plc-hint">Paste a direct stream or radio URL (Icecast/Shoutcast, .mp3, .m3u8, and so on).</p>
      <form method="POST" class="plc-row">
        <input type="hidden" name="csrf_token" value="{{ csrf_token_value }}" />
        <input class="plc-input" type="text" name="identifier" placeholder="https://stream.example.com/live.mp3" />
        <button class="plc-btn" name="action" value="play">Queue</button>
        <button class="plc-btn primary" name="action" value="play_now">Play now</button>
      </form>
    </div>

  </div>

  </div>
{% endif %}
"""
