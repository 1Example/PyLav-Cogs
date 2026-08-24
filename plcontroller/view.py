from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import TYPE_CHECKING

import discord
from redbot.core.i18n import Translator

from pylav.constants.config import DEFAULT_SEARCH_SOURCE
from pylav.extension.red.utils import rgetattr
from pylav.extension.red.utils.decorators import is_dj_logic
from pylav.helpers import emojis
from pylav.players.player import Player
from pylav.type_hints.bot import DISCORD_INTERACTION_TYPE

_ = Translator("PyLavController", Path(__file__))

# Bump this whenever view.py changes. Check what the bot actually loaded with:
#   [p]eval import plcontroller.view as v; print(v.__view_version__)
__view_version__ = "2026.08.14.3-public-queue-menu"

# Discord has no true "transparent" button; secondary/grey is the neutral style
# that blends into the message background. Change this in one place to restyle
# the whole controller.
TRANSPARENT = discord.ButtonStyle.secondary

# How long the queue menu stays open with no interaction before it deletes
# itself. PyLav's default is 600s, which leaves a large panel sitting above
# the controller for ten minutes.
QUEUE_MENU_TIMEOUT = 60

# How long confirmations ("I have skipped ...") stay in the channel before
# they are deleted. These are public now, so they are visible to everyone and
# are removed on this timer rather than lingering per-user.
PUBLIC_DELETE_AFTER = 10

# Queue-menu buttons that reply with an ephemeral confirmation. These all
# defer with thinking=True, so their response is safe to delete. Navigation
# buttons are deliberately excluded -- they defer against the menu message
# itself, and deleting their response would delete the menu.
CONFIRMING_BUTTONS = frozenset(
    {
        "PreviousTrackButton",
        "StopTrackButton",
        "PauseTrackButton",
        "ResumeTrackButton",
        "SkipTrackButton",
        "IncreaseVolumeButton",
        "DecreaseVolumeButton",
        "ToggleRepeatButton",
        "ToggleRepeatQueueButton",
        "ShuffleButton",
        "DisconnectButton",
        "EmptyQueueButton",
        "RemoveFromQueueButton",
        "PlayNowFromQueueButton",
    }
)


class PublicInteractionResponse:
    """Wraps ``interaction.response`` to force replies out of ephemeral mode.

    PyLav's queue-menu buttons hardcode ``defer(ephemeral=True, thinking=True)``.
    Ephemeral messages cannot be deleted on a timer and are only ever visible
    to the clicker, so they pile up in that person's view instead of being
    cleaned up. ``Interaction.response`` is a cached-slot property, so the
    cached value can be swapped for this proxy while the callback runs.
    """

    __slots__ = ("_response",)

    def __init__(self, response):
        self._response = response

    def __getattr__(self, item):
        return getattr(self._response, item)

    async def defer(self, *args, **kwargs):
        kwargs["ephemeral"] = False
        return await self._response.defer(*args, **kwargs)

    async def send_message(self, *args, **kwargs):
        kwargs["ephemeral"] = False
        result = await self._response.send_message(*args, **kwargs)
        return result


class AutoDeletingFollowup:
    """Wraps ``interaction.followup`` so ephemeral replies clean themselves up.

    Red sends command responses through ``interaction.followup.send``. We
    cannot patch the Webhook itself (it uses ``__slots__``), but
    ``Interaction.followup`` is a cached-slot property, so swapping the
    cached value for this proxy lets us schedule a delete on anything sent
    while a button callback is running.
    """

    __slots__ = ("_webhook", "_delay")

    def __init__(self, webhook, delay: float):
        self._webhook = webhook
        self._delay = delay

    def __getattr__(self, item):
        return getattr(self._webhook, item)

    async def send(self, *args, **kwargs):
        kwargs.setdefault("wait", True)
        kwargs["ephemeral"] = False
        message = await self._webhook.send(*args, **kwargs)
        if message is not None:
            with contextlib.suppress(Exception):
                # delay= schedules a background task and returns immediately.
                await message.delete(delay=self._delay)
        return message


if TYPE_CHECKING:
    from plcontroller.cog import PyLavController


class IncreaseVolumeButton(discord.ui.Button):
    def __init__(
        self,
        cog: PyLavController,
        style: discord.ButtonStyle,
        row: int = None,
        custom_id: str | None = None,
        label: str | None = None,
    ):
        super().__init__(
            style=style,
            emoji="🔊",
            label=label,
            row=row,
            custom_id=custom_id,
        )
        self.cog = cog

    async def callback(self, interaction: DISCORD_INTERACTION_TYPE):
        if not interaction.response.is_done():
            await interaction.response.defer()
        context = await self.cog.bot.get_context(interaction)
        await self.cog.volume(context, change_by=5)
        await self.view.update_view()


class DecreaseVolumeButton(discord.ui.Button):
    def __init__(
        self,
        cog: PyLavController,
        style: discord.ButtonStyle,
        row: int = None,
        custom_id: str | None = None,
        label: str | None = None,
    ):
        super().__init__(
            style=style,
            emoji="🔉",
            label=label,
            row=row,
            custom_id=custom_id,
        )
        self.cog = cog

    async def callback(self, interaction: DISCORD_INTERACTION_TYPE):
        if not interaction.response.is_done():
            await interaction.response.defer()
        context = await self.cog.bot.get_context(interaction)
        await self.cog.volume(context, change_by=-5)
        await self.view.update_view()


class StopTrackButton(discord.ui.Button):
    def __init__(
        self,
        cog: PyLavController,
        style: discord.ButtonStyle,
        row: int = None,
        custom_id: str | None = None,
        label: str | None = None,
    ):
        super().__init__(
            style=style,
            emoji="⏹️",
            label=label,
            row=row,
            custom_id=custom_id,
        )
        self.cog = cog

    async def callback(self, interaction: DISCORD_INTERACTION_TYPE):
        if not interaction.response.is_done():
            await interaction.response.defer()
        context = await self.cog.bot.get_context(interaction)
        await self.cog.stop(context)
        await self.view.update_view(forced=True)


class PauseTrackButton(discord.ui.Button):
    def __init__(
        self,
        cog: PyLavController,
        style: discord.ButtonStyle,
        row: int = None,
        custom_id: str | None = None,
        label: str | None = None,
    ):
        super().__init__(
            style=style,
            emoji="⏸️",
            label=label,
            row=row,
            custom_id=custom_id,
        )
        self.cog = cog

    async def callback(self, interaction: DISCORD_INTERACTION_TYPE):
        if not interaction.response.is_done():
            await interaction.response.defer()
        context = await self.cog.bot.get_context(interaction)
        await self.cog.pause(context)
        await self.view.update_view()


class ResumeTrackButton(discord.ui.Button):
    def __init__(
        self,
        cog: PyLavController,
        style: discord.ButtonStyle,
        row: int = None,
        custom_id: str | None = None,
        label: str | None = None,
    ):
        super().__init__(
            style=style,
            emoji="▶️",
            label=label,
            row=row,
            custom_id=custom_id,
        )
        self.cog = cog

    async def callback(self, interaction: DISCORD_INTERACTION_TYPE):
        if not interaction.response.is_done():
            await interaction.response.defer()
        context = await self.cog.bot.get_context(interaction)
        await self.cog.resume(context)
        await self.view.update_view()


class SkipTrackButton(discord.ui.Button):
    def __init__(
        self,
        cog: PyLavController,
        style: discord.ButtonStyle,
        row: int = None,
        custom_id: str | None = None,
        label: str | None = None,
    ):
        super().__init__(
            style=style,
            emoji="⏭️",
            label=label,
            row=row,
            custom_id=custom_id,
        )
        self.cog = cog

    async def callback(self, interaction: DISCORD_INTERACTION_TYPE):
        if not interaction.response.is_done():
            await interaction.response.defer()
        context = await self.cog.bot.get_context(interaction)
        await self.cog.skip(context)
        await self.view.update_view()


class ToggleRepeatButton(discord.ui.Button):
    def __init__(
        self,
        cog: PyLavController,
        style: discord.ButtonStyle,
        row: int = None,
        custom_id: str | None = None,
        label: str | None = None,
    ):
        super().__init__(
            style=style,
            emoji="🔁",
            label=label,
            row=row,
            custom_id=custom_id,
        )
        self.cog = cog

    async def callback(self, interaction: DISCORD_INTERACTION_TYPE):
        if not interaction.response.is_done():
            await interaction.response.defer()
        context = await self.cog.bot.get_context(interaction)
        player = context.player
        if not player:
            return await context.send(
                embed=await self.cog.pylav.construct_embed(
                    description=_("I am not connected to any voice channel at the moment."), messageable=interaction
                ),
                delete_after=PUBLIC_DELETE_AFTER,
            )
        await self.cog.repeat(context, queue=await player.config.fetch_repeat_current())
        await self.view.update_view()


class QueueHistoryButton(discord.ui.Button):
    def __init__(
        self,
        cog: PyLavController,
        style: discord.ButtonStyle,
        row: int = None,
        custom_id: str | None = None,
        label: str | None = None,
    ):
        super().__init__(
            style=style,
            emoji="🕓",
            label=label,
            row=row,
            custom_id=custom_id,
        )
        self.cog = cog

    async def callback(self, interaction: DISCORD_INTERACTION_TYPE):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        player = self.cog.pylav.get_player(interaction.guild.id)
        if player is None:
            return await interaction.followup.send(
                embed=await self.cog.pylav.construct_embed(
                    description=_("I am not connected to any voice channel at the moment."),
                    messageable=interaction,
                ),
                ephemeral=True,
            )

        # List only - the transport controls live on the main controller, so this
        # no longer opens PyLav's full queue menu with a duplicate button panel.
        try:
            history = list(player.history.raw_queue)
        except Exception:  # noqa: BLE001
            history = []
        if not history:
            return await interaction.followup.send(
                embed=await self.cog.pylav.construct_embed(
                    description=_("Nothing has been played yet."), messageable=interaction
                ),
                ephemeral=True,
            )

        lines = []
        for position, track in enumerate(history[:25], start=1):
            try:
                title = await track.title()
                author = await track.author()
            except Exception:  # noqa: BLE001
                title, author = _("Unknown title"), ""
            lines.append(f"`{position:>2}.` **{title}**" + (f" — {author}" if author else ""))

        description = "\n".join(lines)
        if len(history) > 25:
            description += "\n\n" + _("...and {number} more.").format(number=len(history) - 25)

        await interaction.followup.send(
            embed=await self.cog.pylav.construct_embed(
                title=_("Recently played in {guild}").format(guild=interaction.guild.name),
                description=description,
                footer=_("{tracks} track(s)").format(tracks=len(history)),
                messageable=interaction,
            ),
            ephemeral=True,
        )


class QueueButton(discord.ui.Button):
    def __init__(
        self,
        cog: PyLavController,
        style: discord.ButtonStyle,
        row: int = None,
        custom_id: str | None = None,
        label: str | None = None,
    ):
        super().__init__(
            style=style,
            emoji="📜",
            label=label,
            row=row,
            custom_id=custom_id,
        )
        self.cog = cog

    async def callback(self, interaction: DISCORD_INTERACTION_TYPE):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        player = self.cog.pylav.get_player(interaction.guild.id)
        if player is None:
            return await interaction.followup.send(
                embed=await self.cog.pylav.construct_embed(
                    description=_("I am not connected to any voice channel at the moment."),
                    messageable=interaction,
                ),
                ephemeral=True,
            )
        if player.queue.empty():
            return await interaction.followup.send(
                embed=await self.cog.pylav.construct_embed(
                    description=_("There is nothing in the queue."), messageable=interaction
                ),
                ephemeral=True,
            )

        # Deliberately just the list - the transport controls live on the main
        # controller, so this no longer opens a second full control panel.
        lines = []
        total_ms = 0
        raw = list(player.queue.raw_queue)
        for position, track in enumerate(raw[:25], start=1):
            try:
                title = await track.title()
                author = await track.author()
                duration = await track.duration() or 0
            except Exception:  # noqa: BLE001
                title, author, duration = _("Unknown title"), "", 0
            total_ms += duration
            lines.append(f"`{position:>2}.` **{title}**" + (f" — {author}" if author else ""))

        description = "\n".join(lines) or _("There is nothing in the queue.")
        if len(raw) > 25:
            description += "\n\n" + _("...and {number} more.").format(number=len(raw) - 25)

        await interaction.followup.send(
            embed=await self.cog.pylav.construct_embed(
                title=_("Queue for {guild}").format(guild=interaction.guild.name),
                description=description,
                footer=_("{tracks} track(s)").format(tracks=len(raw)),
                messageable=interaction,
            ),
            ephemeral=True,
        )


class ToggleRepeatQueueButton(discord.ui.Button):
    def __init__(
        self,
        cog: PyLavController,
        style: discord.ButtonStyle,
        row: int = None,
        custom_id: str | None = None,
        label: str | None = None,
    ):
        super().__init__(
            style=style,
            emoji="🔂",
            label=label,
            row=row,
            custom_id=custom_id,
        )
        self.cog = cog

    async def callback(self, interaction: DISCORD_INTERACTION_TYPE):
        if not interaction.response.is_done():
            await interaction.response.defer()
        context = await self.cog.bot.get_context(interaction)
        player = context.player
        if not player:
            return await context.send(
                embed=await self.cog.pylav.construct_embed(
                    description=_("I am not connected to any voice channel at the moment."), messageable=interaction
                ),
                delete_after=PUBLIC_DELETE_AFTER,
            )
        repeat_queue = bool(await player.config.fetch_repeat_current())
        await self.cog.repeat(context, queue=repeat_queue)
        await self.view.update_view()


class ShuffleButton(discord.ui.Button):
    def __init__(
        self,
        cog: PyLavController,
        style: discord.ButtonStyle,
        row: int = None,
        custom_id: str | None = None,
        label: str | None = None,
    ):
        super().__init__(
            style=style,
            emoji="🔀",
            label=label,
            row=row,
            custom_id=custom_id,
        )
        self.cog = cog

    async def callback(self, interaction: DISCORD_INTERACTION_TYPE):
        if not interaction.response.is_done():
            await interaction.response.defer()
        context = await self.cog.bot.get_context(interaction)
        await self.cog.shuffle(context)
        await self.view.update_view()


class PreviousTrackButton(discord.ui.Button):
    def __init__(
        self,
        cog: PyLavController,
        style: discord.ButtonStyle,
        row: int = None,
        custom_id: str | None = None,
        label: str | None = None,
    ):
        super().__init__(
            style=style,
            emoji="⏮️",
            label=label,
            row=row,
            custom_id=custom_id,
        )
        self.cog = cog

    async def callback(self, interaction: DISCORD_INTERACTION_TYPE):
        if not interaction.response.is_done():
            await interaction.response.defer()
        context = await self.cog.bot.get_context(interaction)
        await self.cog.previous(context)
        await self.view.update_view()


class RefreshButton(discord.ui.Button):
    def __init__(
        self,
        cog: PyLavController,
        style: discord.ButtonStyle,
        row: int = None,
        custom_id: str | None = None,
        label: str | None = None,
    ):
        super().__init__(
            style=style,
            emoji="🔄",
            label=label,
            row=row,
            custom_id=custom_id,
        )
        self.cog = cog

    async def callback(self, interaction: DISCORD_INTERACTION_TYPE):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        await self.view.update_view()



class ControllerDisconnectButton(discord.ui.Button):
    """Disconnect, acting on the player directly.

    PyLav's queue-view DisconnectButton calls `cog.command_disconnect`, which
    only exists on PyLavPlayer - not on this cog - so it can't be reused here.
    """

    def __init__(
        self,
        cog: PyLavController,
        style: discord.ButtonStyle,
        row: int = None,
        custom_id: str | None = None,
        label: str | None = None,
    ):
        super().__init__(style=style, emoji="\u23cf\ufe0f", label=label, row=row, custom_id=custom_id)
        self.cog = cog

    async def callback(self, interaction: DISCORD_INTERACTION_TYPE):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        player = self.cog.pylav.get_player(interaction.guild.id)
        if player is None:
            return
        await player.disconnect(requester=interaction.user)
        await self.view.update_view()


class ControllerClearQueueButton(discord.ui.Button):
    """Empty the queue without touching the current track."""

    def __init__(
        self,
        cog: PyLavController,
        style: discord.ButtonStyle,
        row: int = None,
        custom_id: str | None = None,
        label: str | None = None,
    ):
        super().__init__(style=style, emoji="\U0001f5d1\ufe0f", label=label, row=row, custom_id=custom_id)
        self.cog = cog

    async def callback(self, interaction: DISCORD_INTERACTION_TYPE):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        player = self.cog.pylav.get_player(interaction.guild.id)
        if player is None:
            return
        player.queue.clear()
        await self.view.update_view()


class PersistentControllerView(discord.ui.View):
    def __init__(
        self,
        cog: PyLavController,
        channel: discord.TextChannel | discord.Thread | discord.VoiceChannel,
        message: discord.Message = None,
    ):
        super().__init__(timeout=None)
        self.cog = cog
        self.message: discord.Message | None = message
        self.channel = channel
        self.guild = channel.guild
        self.__update_view_lock = asyncio.Lock()
        self.__prepare_lock = asyncio.Lock()
        self.__show_help = False

        # Row 0 - playback controls
        self.previous_track_button = PreviousTrackButton(
            style=TRANSPARENT,
            row=0,
            cog=cog,
            custom_id="pylav__pylavcontroller_persistent_view:previous_track_button:9",
        )
        self.paused_button = PauseTrackButton(
            style=TRANSPARENT,
            row=0,
            cog=cog,
            custom_id="pylav__pylavcontroller_persistent_view:paused_button:7",
        )
        self.resume_button = ResumeTrackButton(
            style=TRANSPARENT,
            row=0,
            cog=cog,
            custom_id="pylav__pylavcontroller_persistent_view:resume_button:8",
        )
        self.skip_button = SkipTrackButton(
            style=TRANSPARENT,
            row=0,
            cog=cog,
            custom_id="pylav__pylavcontroller_persistent_view:skip_button:10",
        )
        self.shuffle_button = ShuffleButton(
            style=TRANSPARENT,
            row=0,
            cog=cog,
            custom_id="pylav__pylavcontroller_persistent_view:shuffle_button:11",
        )
        self.stop_button = StopTrackButton(
            style=TRANSPARENT,
            row=0,
            cog=cog,
            custom_id="pylav__pylavcontroller_persistent_view:stop_button:12",
        )

        # Row 1 - volume, repeat and queue
        self.decrease_volume_button = DecreaseVolumeButton(
            style=TRANSPARENT,
            row=1,
            cog=cog,
            custom_id="pylav__pylavcontroller_persistent_view:decrease_volume_button:5",
        )
        self.increase_volume_button = IncreaseVolumeButton(
            style=TRANSPARENT,
            row=1,
            cog=cog,
            custom_id="pylav__pylavcontroller_persistent_view:increase_volume_button:6",
        )
        self.repeat_queue_button_on = ToggleRepeatQueueButton(
            style=TRANSPARENT,
            row=1,
            cog=cog,
            custom_id="pylav__pylavcontroller_persistent_view:repeat_queue_button_on:1",
        )
        self.repeat_button_on = ToggleRepeatButton(
            style=TRANSPARENT,
            row=1,
            cog=cog,
            custom_id="pylav__pylavcontroller_persistent_view:repeat_button_on:2",
        )
        self.repeat_button_off = ToggleRepeatButton(
            style=TRANSPARENT,
            row=1,
            cog=cog,
            custom_id="pylav__pylavcontroller_persistent_view:repeat_button_off:3",
        )
        self.queue_button = QueueButton(
            style=TRANSPARENT,
            row=1,
            cog=cog,
            custom_id="pylav__pylavcontroller_persistent_view:queue_button:14",
        )
        self.show_history_button = QueueHistoryButton(
            style=TRANSPARENT,
            row=1,
            cog=cog,
            custom_id="pylav__pylavcontroller_persistent_view:show_history_button:4",
        )

        # Row 2 - utility
        self.clear_queue_button = ControllerClearQueueButton(
            style=TRANSPARENT,
            row=2,
            cog=cog,
            custom_id="pylav__pylavcontroller_persistent_view:clear_queue_button:15",
        )
        self.disconnect_button = ControllerDisconnectButton(
            style=TRANSPARENT,
            row=2,
            cog=cog,
            custom_id="pylav__pylavcontroller_persistent_view:disconnect_button:16",
        )
        self.refresh_button = RefreshButton(
            style=TRANSPARENT,
            row=2,
            cog=cog,
            custom_id="pylav__pylavcontroller_persistent_view:refresh_button:13",
        )

    def set_message(self, message: discord.Message):
        self.message = message

    def enable_show_help(self) -> None:
        self.__show_help = True

    def disable_show_help(self) -> None:
        self.__show_help = False

    async def enable_slow_mode(self) -> None:
        if self.channel.slowmode_delay != 0:
            return
        await self.channel.edit(slowmode_delay=5)

    async def disable_slow_mode(self) -> None:
        if self.channel.slowmode_delay == 0:
            return
        await self.channel.edit(slowmode_delay=0)

    async def set_permissions(self):
        if isinstance(self.channel, discord.Thread):
            # Threads don't have permissions, so we can't set them
            #    We don't want to edit the permissions of the parent channel
            #    as that would affect the entire channel and all its threads.
            return
        permissions = self.channel.permissions_for(self.channel.guild.me)
        if permissions.manage_roles or self.guild.me.guild_permissions.manage_roles:
            default_role_permissions = self.channel.permissions_for(self.channel.guild.default_role)
            if not all(
                [
                    default_role_permissions.view_channel,
                    default_role_permissions.read_messages,
                    default_role_permissions.send_messages,
                    default_role_permissions.read_message_history,
                ]
            ) or any(
                [
                    default_role_permissions.create_instant_invite,
                    default_role_permissions.manage_channels,
                    default_role_permissions.add_reactions,
                    default_role_permissions.send_tts_messages,
                    default_role_permissions.manage_messages,
                    default_role_permissions.embed_links,
                    default_role_permissions.attach_files,
                    default_role_permissions.mention_everyone,
                    default_role_permissions.external_emojis,
                    default_role_permissions.manage_roles,
                    default_role_permissions.manage_webhooks,
                    default_role_permissions.use_application_commands,
                    default_role_permissions.create_public_threads,
                    default_role_permissions.create_private_threads,
                    default_role_permissions.external_stickers,
                    default_role_permissions.send_messages_in_threads,
                    default_role_permissions.manage_events,
                    default_role_permissions.manage_threads,
                    default_role_permissions.use_embedded_activities,
                ]
            ):
                with contextlib.suppress(discord.Forbidden):
                    # No explicitly needed; However, just here to allow for a cleaner channel.
                    await self.channel.set_permissions(
                        self.channel.guild.default_role,
                        view_channel=True,
                        read_messages=True,
                        send_messages=True,
                        read_message_history=True,
                        create_instant_invite=False,
                        manage_channels=False,
                        add_reactions=False,
                        send_tts_messages=False,
                        manage_messages=False,
                        embed_links=False,
                        attach_files=False,
                        mention_everyone=False,
                        external_emojis=False,
                        manage_roles=False,
                        manage_webhooks=False,
                        use_application_commands=False,
                        create_public_threads=False,
                        create_private_threads=False,
                        external_stickers=False,
                        send_messages_in_threads=False,
                        manage_events=False,
                        manage_threads=False,
                        use_embedded_activities=False,
                        reason=_("PyLav Controller"),
                    )

    async def prepare(self):
        async with self.__prepare_lock:
            player = self.cog.pylav.get_player(self.channel.guild.id)
            self.clear_items()
            self.show_history_button.disabled = False
            self.queue_button.disabled = False
            self.repeat_button_on.disabled = False
            self.repeat_button_off.disabled = False
            self.repeat_queue_button_on.disabled = False
            self.decrease_volume_button.disabled = False
            self.increase_volume_button.disabled = False
            self.refresh_button.disabled = False
            self.paused_button.disabled = False
            self.resume_button.disabled = False
            self.previous_track_button.disabled = False
            self.skip_button.disabled = False
            self.shuffle_button.disabled = False
            self.stop_button.disabled = False

            # Row 0 - playback controls
            self.add_item(self.previous_track_button)
            if player is not None and player.paused or player is None:
                self.add_item(self.resume_button)
            else:
                self.add_item(self.paused_button)
            self.add_item(self.skip_button)
            self.add_item(self.shuffle_button)
            self.add_item(self.stop_button)

            # Row 1 - volume, repeat and queue
            self.add_item(self.decrease_volume_button)
            self.add_item(self.increase_volume_button)
            if (player is not None) and (repeat_current := await player.config.fetch_repeat_current()):
                self.add_item(self.repeat_button_on)
            elif (player is not None) and (not repeat_current) and (await player.config.fetch_repeat_queue()):
                self.add_item(self.repeat_queue_button_on)
            else:
                self.add_item(self.repeat_button_off)
            self.add_item(self.queue_button)
            self.add_item(self.show_history_button)

            # Row 2 - utility (queue-view controls merged into the main view)
            self.add_item(self.refresh_button)
            self.add_item(self.clear_queue_button)
            self.add_item(self.disconnect_button)

            if player is None:
                self.show_history_button.disabled = True
                self.queue_button.disabled = True
                self.repeat_button_off.disabled = True
                self.decrease_volume_button.disabled = True
                self.increase_volume_button.disabled = True

                self.resume_button.disabled = True
                self.previous_track_button.disabled = True
                self.skip_button.disabled = True
                self.shuffle_button.disabled = True

                self.stop_button.disabled = True
                return

            if player.queue.empty():
                self.shuffle_button.disabled = True
            if not player.current:
                self.stop_button.disabled = True

            if player.history.empty():
                self.previous_track_button.disabled = True
                self.show_history_button.disabled = True

    async def get_player(self, message: discord.Message) -> Player | None:
        if not await is_dj_logic(message, bot=self.cog.bot):
            await message.channel.send(
                embed=await self.cog.pylav.construct_embed(
                    description=_("You need to be a disc jockey in this server to play tracks in this server."),
                    messageable=message.channel,
                ),
                delete_after=10,
            )
            return None
        if (player := self.cog.pylav.get_player(self.guild.id)) is None:
            config = self.cog.pylav.player_config_manager.get_config(self.guild.id)
            if (channel := self.guild.get_channel_or_thread(await config.fetch_forced_channel_id())) is None:
                channel = rgetattr(message, "author.voice.channel", None)
                if not channel:
                    await message.channel.send(
                        embed=await self.cog.pylav.construct_embed(
                            messageable=self.channel,
                            description=_("You must be in a voice channel, so I can connect to it."),
                        ),
                        delete_after=10,
                    )
                    return
            if not ((permission := channel.permissions_for(self.guild.me)) and permission.connect and permission.speak):
                await message.channel.send(
                    embed=await self.cog.pylav.construct_embed(
                        description=_(
                            "I do not have permission to connect or speak in {channel_variable_do_not_translate}."
                        ).format(channel_variable_do_not_translate=channel.mention),
                        messageable=message.channel,
                    ),
                    delete_after=10,
                )
                return
            player = await self.cog.pylav.player_manager.create(channel=channel)
        return player

    async def get_now_playing_embed(self, forced: bool = False) -> dict[str, discord.Embed | str | discord.File]:
        await asyncio.sleep(1)
        player = self.cog.pylav.get_player(self.guild.id)
        if player is None or player.current is None or forced:
            if self.__show_help:
                footer_text = _(
                    "\n\nYou can search specific services by using the following prefixes:\n"
                    "{deezer_service_variable_do_not_translate}  - Deezer\n"
                    "{spotify_service_variable_do_not_translate}  - Spotify\n"
                    "{apple_music_service_variable_do_not_translate}  - Apple Music\n"
                    "{youtube_music_service_variable_do_not_translate} - YouTube Music\n"
                    "{youtube_service_variable_do_not_translate}  - YouTube\n"
                    "{soundcloud_service_variable_do_not_translate}  - SoundCloud\n"
                    "{yandex_music_service_variable_do_not_translate}  - Yandex Music\n"
                    "Example: {example_variable_do_not_translate}.\n\n"
                    "If no prefix is used I will default to {fallback_service_variable_do_not_translate}\n"
                ).format(
                    fallback_service_variable_do_not_translate=f"`{DEFAULT_SEARCH_SOURCE}:`",
                    deezer_service_variable_do_not_translate="'dzsearch:' ",
                    spotify_service_variable_do_not_translate="'spsearch:' ",
                    apple_music_service_variable_do_not_translate="'amsearch:' ",
                    youtube_music_service_variable_do_not_translate="'ytmsearch:'",
                    youtube_service_variable_do_not_translate="'ytsearch:' ",
                    soundcloud_service_variable_do_not_translate="'scsearch:' ",
                    yandex_music_service_variable_do_not_translate="'ymsearch:' ",
                    example_variable_do_not_translate=f"'{DEFAULT_SEARCH_SOURCE}:Hello Adele'",
                )
            else:
                footer_text = None

            return {
                "embed": await self.cog.pylav.construct_embed(
                    description=_("I am not currently playing anything on this server."),
                    messageable=self.channel,
                    footer=footer_text,
                )
            }
        return {"embed": await self._build_now_playing_embed(player)}

    @staticmethod
    def _progress_bar(position: float, duration: float, length: int = 18) -> str:
        if not duration or duration <= 0:
            return "\u25b6\ufe0f  " + ("\u2501" * length) + "  \U0001f507 LIVE"
        ratio = max(0.0, min(1.0, position / duration))
        # Clamp so the knob always fits inside the bar; otherwise a track at
        # exactly 100% renders one character wider than every other frame.
        filled = min(int(ratio * length), length - 1)
        return "\u2501" * filled + "\U0001f518" + "\u2501" * (length - filled - 1)

    @staticmethod
    def _fmt(milliseconds: float | int | None) -> str:
        if not milliseconds or milliseconds < 0:
            return "0:00"
        total = int(milliseconds // 1000)
        hours, rem = divmod(total, 3600)
        minutes, seconds = divmod(rem, 60)
        return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"

    async def _build_now_playing_embed(self, player) -> discord.Embed:
        """Custom Now Playing card.

        Replaces PyLav's get_currently_playing_message(), which rendered a long
        block of search-prefix help and stacked every field vertically.
        """
        track = player.current

        async def safe(coro, default=""):
            try:
                value = await coro()
            except Exception:  # noqa: BLE001
                return default
            return default if value is None else value

        title = await safe(track.title, _("Unknown title"))
        author = await safe(track.author)
        uri = await safe(track.uri)
        artwork = await safe(track.artworkUrl)
        duration = await safe(track.duration, 0)
        is_stream = bool(await safe(track.stream, False))
        try:
            position = await player.position()
        except Exception:  # noqa: BLE001
            position = 0

        heading = f"[{title}]({uri})" if uri else f"**{title}**"
        description = [heading]
        if author:
            description.append(f"-# {author}")

        bar = self._progress_bar(position, 0 if is_stream else duration)
        if is_stream:
            description.append(f"\n{bar}")
        else:
            description.append(f"\n`{self._fmt(position)}` {bar} `{self._fmt(duration)}`")

        embed = discord.Embed(
            description="\n".join(description),
            colour=await self.cog.bot.get_embed_colour(self.channel),
        )
        embed.set_author(
            name=_("Now playing in {guild}").format(guild=self.guild.name),
            icon_url=self.guild.icon.url if self.guild.icon else None,
        )
        if artwork:
            embed.set_thumbnail(url=artwork)

        requester = self.cog.bot.get_user(getattr(track, "requester_id", 0))
        embed.add_field(
            name=_("Requested by"),
            value=requester.mention if requester else _("Unknown"),
            inline=True,
        )
        embed.add_field(name=_("Volume"), value=f"{player.volume}%", inline=True)

        try:
            repeat_current = await player.config.fetch_repeat_current()
            repeat_queue = await player.config.fetch_repeat_queue()
        except Exception:  # noqa: BLE001
            repeat_current = repeat_queue = False
        repeat = _("Track") if repeat_current else (_("Queue") if repeat_queue else _("Off"))
        embed.add_field(name=_("Repeat"), value=repeat, inline=True)

        # Autoplay / effects / total queue time round out the summary.
        try:
            autoplay = await player.autoplay_enabled()
        except Exception:  # noqa: BLE001
            autoplay = False
        embed.add_field(
            name=_("Autoplay"), value=_("On") if autoplay else _("Off"), inline=True
        )

        raw = list(player.queue.raw_queue)
        remaining = 0
        for entry in raw:
            try:
                remaining += await entry.duration() or 0
            except Exception:  # noqa: BLE001
                continue
        embed.add_field(
            name=_("Queue length"),
            value=_("{count} track(s) · {time}").format(count=len(raw), time=self._fmt(remaining)),
            inline=True,
        )
        embed.add_field(name="\u200b", value="\u200b", inline=True)

        if raw:
            upcoming = []
            for nxt in raw[:3]:
                try:
                    upcoming.append(f"- {await nxt.title()}")
                except Exception:  # noqa: BLE001
                    continue
            embed.add_field(
                name=_("Up next ({count} queued)").format(count=len(raw)),
                value="\n".join(upcoming) or _("Nothing"),
                inline=False,
            )
        else:
            embed.add_field(name=_("Up next"), value=_("Queue is empty"), inline=False)

        try:
            base_url = await self.cog._config.dashboard_url()
        except Exception:  # noqa: BLE001
            base_url = ""
        if base_url:
            embed.add_field(
                name=_("Web player"),
                value=f"[{_('Open the dashboard controller')}]"
                f"({base_url}/dashboard/{self.guild.id}/third-party/PyLavController)",
                inline=False,
            )

        channel = getattr(player, "channel", None)
        if channel is not None:
            embed.set_footer(text=_("Connected to {channel}").format(channel=channel.name))
        return embed

    async def update_view(self, forced: bool = False):
        async with self.__update_view_lock:
            await self.prepare()
            kwargs = await self.get_now_playing_embed(forced)
            attachments = []
            if "file" in kwargs:
                attachments = [kwargs.pop("file")]
            elif "files" in kwargs:
                attachments = kwargs.pop("files")
            if attachments:
                kwargs["attachments"] = attachments
            await self.message.edit(view=self, **kwargs)

    # Buttons every listener may press. Mirrors LISTENER_ACTIONS on the web
    # dashboard so both surfaces enforce the same rules.
    LISTENER_BUTTONS = (
        "previous_track_button",
        "paused_button",
        "resume_button",
        "skip_button",
        "shuffle_button",
        "decrease_volume_button",
        "increase_volume_button",
        "queue_button",
        "show_history_button",
        "refresh_button",
    )

    async def interaction_check(self, interaction: DISCORD_INTERACTION_TYPE, /) -> bool:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

        # custom_id format: "pylav__pylavcontroller_persistent_view:<name>:<n>"
        custom_id = (interaction.data or {}).get("custom_id", "")
        parts = custom_id.split(":")
        button_name = parts[1] if len(parts) > 2 else ""
        listener_allowed = button_name in self.LISTENER_BUTTONS

        if not listener_allowed and not await is_dj_logic(interaction):
            await interaction.send(
                embed=await interaction.client.pylav.construct_embed(
                    description=_(
                        "Only a disc jockey can do that. You can still play, pause, skip, "
                        "shuffle and adjust the volume."
                    ),
                    messageable=interaction,
                ),
                delete_after=PUBLIC_DELETE_AFTER,
            )
            return False
        if not (self.cog.pylav.get_player(self.channel.guild.id)):
            await interaction.send(
                embed=await interaction.client.pylav.construct_embed(
                    description=_("I am not currently playing anything on this server."),
                    messageable=interaction,
                ),
                delete_after=PUBLIC_DELETE_AFTER,
            )
            return False
        return True
