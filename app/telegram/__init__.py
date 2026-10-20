"""Telegram bot, Mini App auth, and delivery helpers for MoneyPrinterTurbo."""

__all__ = ["load_telegram_settings"]


def load_telegram_settings():
    from app.telegram.settings import load_telegram_settings as _load

    return _load()
