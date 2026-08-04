"""Logging configuration and error reporting utilities."""

import logging
import logging.handlers
import os
import traceback
from pathlib import Path
from typing import Optional

from src.config import load_settings


def setup_logging(log_level: str = "INFO", log_file: Optional[str] = None) -> None:
    """
    Configure logging with file and console handlers.
    
    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_file: Path to log file. If None, uses 'logs/bot.log' in project root.
    """
    # Create logs directory if it doesn't exist
    if log_file is None:
        log_dir = Path(__file__).parent.parent.parent / "logs"
        log_dir.mkdir(exist_ok=True)
        log_file = str(log_dir / "bot.log")
    else:
        log_file_path = Path(log_file)
        log_file_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Get log level
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)
    
    # Create formatters
    detailed_formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    simple_formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%H:%M:%S'
    )
    
    # Root logger configuration
    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)
    
    # Remove existing handlers to avoid duplicates
    root_logger.handlers.clear()
    
    # File handler with rotation (10MB per file, keep 5 backups)
    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5,
        encoding='utf-8'
    )
    file_handler.setLevel(numeric_level)
    file_handler.setFormatter(detailed_formatter)
    root_logger.addHandler(file_handler)
    
    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(numeric_level)
    console_handler.setFormatter(simple_formatter)
    root_logger.addHandler(console_handler)
    
    # Set levels for third-party libraries
    logging.getLogger("aiogram").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy").setLevel(logging.WARNING)
    
    logging.info(f"Logging configured. Log file: {log_file}")


async def send_error_to_admin(
    bot,
    error: Exception,
    context: Optional[str] = None,
    include_traceback: bool = True,
    exc_info: Optional[tuple] = None
) -> None:
    """
    Send error notification to admin chat.
    
    Args:
        bot: Bot instance
        error: Exception that occurred
        context: Additional context about where error occurred
        include_traceback: Whether to include full traceback
        exc_info: Exception info tuple (from sys.exc_info() or logger.exc_info)
    """
    try:
        settings = load_settings()
        
        error_type = type(error).__name__
        error_message = str(error)
        
        # Build error message
        # This goes to the operators' chat, so it follows the instance UI
        # language like everything else they read — not the customer's.
        from src.dashboard.i18n import t as _op
        message_parts = [
            f"🚨 *{_op('bot_crash_title')}*",
            "",
            f"*{_op('bot_crash_type')}* `{error_type}`",
            f"*{_op('bot_crash_message')}* `{error_message}`",
        ]

        if context:
            message_parts.append(f"*{_op('bot_crash_context')}* {context}")
        
        if include_traceback:
            # Get traceback
            if exc_info:
                tb = "".join(traceback.format_exception(*exc_info))
            else:
                # Try to get traceback from exception
                tb_lines = traceback.format_exception(type(error), error, error.__traceback__)
                tb = "".join(tb_lines)
            
            # Limit traceback to last 1500 characters to avoid message too long
            if len(tb) > 1500:
                tb = "..." + tb[-1500:]
            message_parts.extend([
                "",
                "*Traceback:*",
                f"```\n{tb}\n```",
            ])
        
        error_text = "\n".join(message_parts)
        
        # Try to send error notification
        try:
            await bot.send_message(
                chat_id=settings.admin_chat_id,
                text=error_text,
                parse_mode="Markdown",
            )
        except Exception as send_error:
            # If we can't send to admin, log it
            logging.error(f"Failed to send error notification to admin: {send_error}")
            logging.error(f"Original error: {error_type}: {error_message}")
            if include_traceback:
                logging.error(f"Traceback:\n{traceback.format_exc()}")
    
    except Exception as e:
        # Last resort - just log to file
        logging.critical(f"Failed to send error notification: {e}")
        logging.critical(f"Original error: {error}")


def get_logger(name: str) -> logging.Logger:
    """Get a logger instance for a module."""
    return logging.getLogger(name)

