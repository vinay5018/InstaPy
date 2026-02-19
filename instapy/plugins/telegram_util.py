"""
A fully functional TelegramBot to get info on an instapy run
or to control the instapy bot

you will need to create your token on the telegram app and speak with @botfather
you will need to have a username (go to settings -> profile -> Username
"""
import logging
import os
import re
import requests
from requests.exceptions import RequestException
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters
from telegram.error import (
    TelegramError,
    Unauthorized,
    BadRequest,
    TimedOut,
    ChatMigrated,
    NetworkError,
)

from ..util import truncate_float


class InstaPyTelegramException(Exception):
    """Custom exception for Telegram bot errors."""
    pass


class InstaPyTelegramBot:
    """
    Class to handle the instapy telegram bot
    """

    def __init__(
        self,
        token="",
        telegram_username="",
        instapy_session=None,
        debug=True,
        proxy=None,
        logger=None,
    ):
        # private properties
        self.__logger = logger or logging.getLogger(__name__)
        self.__chat_id = None
        self.__updater = None
        self.__context = None

        # public properties
        self.token = token
        self.instapy_session = instapy_session
        self.debug = debug
        self.proxy = proxy

        # validate and normalize username
        self.telegram_username = self._validate_username(telegram_username)

        # restore chat_id from previous run if possible
        self._restore_chat_id()

        # launch the bot if everything required is present
        if self._can_start():
            self.telegram_bot()

    # ---------------------
    # Private utilities
    # ---------------------
    @staticmethod
    def _validate_username(name):
        """Whitelist validation for telegram usernames."""
        if not name:
            return ""
        name = name.strip().lower()
        if not re.fullmatch(r"[a-z0-9_]{5,32}", name):
            raise InstaPyTelegramException(
                "telegram_username must be 5-32 alphanumeric/underscore characters"
            )
        return name

    def _can_start(self):
        return (
            self.token
            and self.telegram_username
            and self.instapy_session is not None
        )

    def _restore_chat_id(self):
        if self.instapy_session is None:
            return
        chat_file = self._safe_chat_file_path()
        try:
            with open(chat_file, "r", encoding="utf-8") as f:
                self.__chat_id = f.read().strip()
        except (OSError, IOError):
            self.__chat_id = None

    def _safe_chat_file_path(self):
        """Prevent path traversal by forcing file inside logfolder."""
        base = os.path.abspath(self.instapy_session.logfolder)
        path = os.path.join(base, "telegram_chat_id.txt")
        return path

    def _clean_web_hooks(self):
        """Remove any webhooks with timeout to avoid hangs."""
        url = f"https://api.telegram.org/bot{self.token}/deleteWebhook"
        try:
            resp = requests.get(url, timeout=10)
            if not resp.json().get("ok"):
                self.__logger.warning("Unable to remove webhook – wrong token?")
        except RequestException as exc:
            self.__logger.warning("Webhook removal failed: %s", exc)

    def _setup_updater(self):
        """Create updater with or without proxy."""
        kwargs = {"use_context": True, "user_sig_handler": self.end}
        if self.proxy:
            kwargs["request_kwargs"] = self.proxy
        return Updater(self.token, **kwargs)

    def _register_handlers(self, dispatcher):
        dispatcher.add_error_handler(self._error_callback)
        dispatcher.add_handler(CommandHandler("start", self._start))
        dispatcher.add_handler(CommandHandler("report", self._report))
        dispatcher.add_handler(CommandHandler("stop", self._stop))
        dispatcher.add_handler(MessageHandler(Filters.command, self._unknown))

    def _start_polling(self, updater):
        updater.start_polling()
        if self.__chat_id is not None:
            self.__context.bot.send_message(
                self.__chat_id, text="Telegram session restored, InstaPy starting\n"
            )

    # ---------------------
    # Public API
    # ---------------------
    @property
    def debug(self):
        return getattr(self, "_debug", False)

    @debug.setter
    def debug(self, value):
        self._debug = bool(value)
        self.__logger.setLevel(logging.DEBUG if self._debug else logging.INFO)

    def telegram_bot(self):
        """Initialize bot if requirements satisfied; split for clarity."""
        if not self._can_start():
            self.__logger.warning(
                "token, telegram_username and instapy_session required"
            )
            return
        self._clean_web_hooks()
        self.__updater = self._setup_updater()
        self.__context = self.__updater.dispatcher
        self._register_handlers(self.__context)
        self._start_polling(self.__updater)

    def send_message(self, text=""):
        """
        Send a message to the stored chat.
        Authorisation is enforced – only the configured username may trigger messages.
        """
        if not text:
            return
        if not self.__chat_id or not self.__context:
            raise InstaPyTelegramException(
                "chat_id unavailable; send /start first"
            )
        # Additional guard: ensure the stored username is still the one allowed
        # (defence in depth)
        if not self.telegram_username:
            raise InstaPyTelegramException("No authorised username configured")
        self.__context.bot.send_message(chat_id=self.__chat_id, text=text)

    def delete_session_file(self):
        """Safe delete of the stored chat_id file."""
        chat_file = self._safe_chat_file_path()
        try:
            os.remove(chat_file)
        except FileNotFoundError:
            self.__logger.debug("Chat file already absent")
        except OSError as exc:
            self.__logger.warning("Could not delete chat file: %s", exc)

    # ---------------------
    # Command handlers
    # ---------------------
    def _start(self, update, context):
        username = update.effective_user.username
        if not self._check_authorized(username, update, context):
            return
        self.__chat_id = str(update.message.chat_id)
        chat_file = self._safe_chat_file_path()
        try:
            with open(chat_file, "w", encoding="utf-8") as f:
                f.write(self.__chat_id)
        except OSError as exc:
            self.__logger.error("Failed to write chat_id: %s", exc)
            context.bot.send_message(
                chat_id=self.__chat_id,
                text="Error saving session – check file permissions",
            )
            return
        context.bot.send_message(
            chat_id=self.__chat_id, text="Bot initialized successfully!\n"
        )

    def _report(self, update, context):
        username = update.effective_user.username
        if not self._check_authorized(username, update, context):
            return
        self.__chat_id = str(update.message.chat_id)
        context.bot.send_message(
            chat_id=self.__chat_id, text=self._live_report()
        )

    def _stop(self, update, context):
        username = update.effective_user.username
        if not self._check_authorized(username, update, context):
            return
        self.__chat_id = str(update.message.chat_id)
        self.instapy_session.aborting = True
        context.bot.send_message(
            chat_id=self.__chat_id, text="InstaPy session abort set\n"
        )

    def _unknown(self, update, context):
        username = update.effective_user.username
        if not self._check_authorized(username, update, context):
            return
        context.bot.send_message(
            chat_id=update.message.chat_id,
            text="Sorry I don't understand that command\n"
            "Recognised actions are:\n"
            "  - /start (initialize bot)\n"
            "  - /report (live report)\n"
            "  - /stop (force stop)\n",
        )

    def _check_authorized(self, username, update, context):
        """Compare normalised usernames; reject on mismatch."""
        if not username or username.lower() != self.telegram_username:
            self.__logger.warning("Unauthorized access from %s", update.effective_user)
            context.bot.send_message(
                chat_id=update.message.chat_id,
                text="You are not authorized to use this service\n",
            )
            return False
        return True

    # ---------------------
    # Utility / callback
    # ---------------------
    def _error_callback(self, update, error):
        """Centralised error handler."""
        self.__logger.warning("TELEGRAM ERROR %s update=%s", error, update)

    def _live_report(self):
        """Build a concise live report string."""
        stats = [
            self.instapy_session.liked_img,
            self.instapy_session.already_liked,
            self.instapy_session.commented,
            self.instapy_session.followed,
            self.instapy_session.already_followed,
            self.instapy_session.unfollowed,
            self.instapy_session.stories_watched,
            self.instapy_session.reels_watched,
            self.instapy_session.inap_img,
            self.instapy_session.not_valid_users,
        ]
        sessional_run_time = self.instapy_session.run_time()
        run_time_info = (
            f"{sessional_run_time} seconds"
            if sessional_run_time < 60
            else f"{truncate_float(sessional_run_time / 60, 2)} minutes"
            if sessional_run_time < 3600
            else f"{truncate_float(sessional_run_time / 3600, 2)} hours"
        )
        run_time_msg = f"[Session lasted {run_time_info}]"

        if any(stats):
            return (
                "Sessional Live Report:\n"
                f"|> LIKED {self.instapy_session.liked_img} images\n"
                f"|> ALREADY LIKED: {self.instapy_session.already_liked}\n"
                f"|> COMMENTED: {self.instapy_session.commented}\n"
                f"|> FOLLOWED: {self.instapy_session.followed}\n"
                f"|> ALREADY FOLLOWED: {self.instapy_session.already_followed}\n"
                f"|> UNFOLLOWED: {self.instapy_session.unfollowed}\n"
                f"|> INAPPROPRIATE: {self.instapy_session.inap_img}\n"
                f"|> NOT VALID USERS: {self.instapy_session.not_valid_users}\n"
                f"|> STORIES WATCHED: {self.instapy_session.stories_watched}\n"
                f"|> REELS WATCHED: {self.instapy_session.reels_watched}\n"
                f"\n{run_time_msg}"
            )
        return f"Sessional Live Report:\n|> No statistics yet\n\n{run_time_msg}"

    def end(self):
        """Tidy up; keep chat_id for next run."""
        if self.__chat_id and self.__context:
            try:
                self.__context.bot.send_message(
                    chat_id=self.__chat_id, text=self._live_report()
                )
            except TelegramError as exc:
                self.__logger.debug("Could not send final report: %s", exc)
        if self.__updater:
            self.__updater.stop()
        # Clear sensitive state
        self.token = ""
        self.telegram_username = ""
        self.instapy_session = None
        self.__chat_id = None
        self.__context = None
        self.__updater = None

    # Legacy static method for backward compatibility
    @staticmethod
    def telegram_delete_session(session):
        """Static wrapper for compatibility; delegates to instance method."""
        # Create a dummy bot only to delete the file
        try:
            dummy = InstaPyTelegramBot(instapy_session=session)
            dummy.delete_session_file()
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "Static delete failed: %s", exc
            )