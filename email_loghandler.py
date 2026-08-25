"""
email_log_handler.py

A logging handler that buffers log messages and sends them as an email at program
termination (or immediately on request). SMTP/SSL/TLS/Auth configurable. Subject,
header, and footer freely definable. Optional TRACE level included.

Usage:
    import logging
    from email_log_handler import make_email_handler, TRACE

    logger = logging.getLogger("my_app_logger")
    logger.setLevel(TRACE)  # Globally allow TRACE and above

    handler = make_email_handler(
        smtp_host="smtp.example.com",
        smtp_port=587,
        use_tls=True,
        username="username@example.com",
        password="***PASSWORD***",
        from_addr="username@example.com",
        to_addrs=["receiver@example.com"],
        subject_template="[Log-Report] {program_name} – {count} entries (max: {levelname_max})",
        header="Hello {program_name} user,\n\nhere is the log excerpt:\n",
        footer="\n--\nAutomatic dispatch @ {hostname} / {date:%Y-%m-%d %H:%M}",
        level=logging.WARNING,               # From WARNING upward in the mail body
        attachment_min_level=logging.DEBUG,  # But send full log from DEBUG upward as attachment
    )

    logger.addHandler(handler)

    logger.info("All ready.")
    logger.warning("Attention, something looks suspicious.")
    # No explicit flush() needed – sending happens automatically at the end.

Debug-friendly behavior:
- In the debugger (sys.gettrace() != None) NO atexit callback is registered.
  Sending then happens via logging.shutdown() -> handler.close() (more robust).
- Environment variables:
    NO_ATEXIT=1   -> forces: no atexit registration
    SMTP_DEBUG=1  -> output SMTP dialog to stderr

Author: (c) 2026 jstiete (https://github.com/jstiete)
"""

from __future__ import annotations

import atexit
import os
import socket
import sys
import smtplib
import threading
from datetime import datetime
import logging
from typing import List, Optional, Sequence, Union
from email.message import EmailMessage


# ---------------------------------------------------------------------------
# Optional TRACE level (below DEBUG)
# Usage: logger.trace("...") and handler.setLevel(TRACE)
# ---------------------------------------------------------------------------
TRACE = 5
if logging.getLevelName(TRACE) == "Level %s" % TRACE:
    logging.addLevelName(TRACE, "TRACE")

    def _trace(self, msg, *args, **kwargs):
        if self.isEnabledFor(TRACE):
            self._log(TRACE, msg, args, **kwargs)

    logging.Logger.trace = _trace  # type: ignore


def _ensure_list(x: Union[str, Sequence[str], None]) -> List[str]:
    if x is None:
        return []
    if isinstance(x, str):
        return [x]
    return list(x)


def _should_register_atexit(default: bool = True) -> bool:
    """
    Determines whether the atexit callback should be registered.
    - disabled if NO_ATEXIT=1 is set
    - disabled if a debugger is attached (sys.gettrace() != None)
    """
    try:
        if os.environ.get("NO_ATEXIT", "").strip() == "1":
            return False
        if sys.gettrace() is not None:  # Debugger active
            return False
    except Exception:
        pass
    return default


class BufferedSMTPHandler(logging.Handler):
    """
    An extended logging handler that collects log entries and automatically sends
    them via email at program termination. In addition to the normal email body,
    which is filtered according to the configured logging level, a complete log
    history (e.g., from DEBUG level upward) can optionally be sent as a UTF‑8
    encoded file attachment.

    The email fully supports UTF‑8 (including special characters, Unicode, and emojis),
    both in the message text and in the attachment.

    The email is automatically sent by default:
        - at the end of the Python interpreter via logging.shutdown() → handler.close()
        - (optionally) additionally via an atexit callback (not in debug mode)
    Therefore, no explicit function call is required for sending.

    Main features:
    - Buffering of log entries until program termination
    - Sending an email whose content is filtered according to handler.level
    - Optional: full log history as UTF‑8 file attachment ("full_log.txt")
    - Freely definable subject, header, and footer (with placeholders)
    - Configurable trace/minimum level
    - SSL or TLS SMTP support
    - Optional: send when a buffer threshold (max_buffer) is reached
    - Robust error handling (logging must not break the program)
    - Debug-robust: no atexit in debugger, `close()` catches BaseException (KeyboardInterrupt/SystemExit)

    Placeholders for subject_template, header, footer:
        {count}            Number of entries in the email body
        {levelname_max}    Highest occurring level name in the mail
        {levelno_max}      Highest occurring level number
        {program_name}     Current script/program name
        {hostname}         Machine hostname
        {date}             Sending timestamp (datetime object)
        {min_level}        Current filter level of the email body

    Parameters:
        smtp_host (str):
            Address of the SMTP server.

        smtp_port (int, default=587):
            Port of the SMTP server. Typical values:
                587 → STARTTLS
                465 → SSL directly
                25  → unencrypted (not recommended)

        username (str | None):
            Username for SMTP auth (optional).

        password (str | None):
            SMTP password (optional).

        use_tls (bool, default=True):
            Enables STARTTLS.
            Note: do not use together with use_ssl.

        use_ssl (bool, default=False):
            SMTP over SSL (port 465).
            Note: use_ssl and use_tls are mutually exclusive.

        timeout (float | None):
            Timeout for the SMTP connection in seconds.

        from_addr (str):
            Sender address of the email.

        to_addrs (str | list[str]):
            Recipient addresses (TO).

        cc_addrs (str | list[str] | None):
            Carbon-Copy (CC) recipients.

        bcc_addrs (str | list[str] | None):
            Blind-Carbon-Copy (BCC) recipients.

        subject_template (str):
            Subject template (placeholders supported).

        header (str | None):
            Text block above the log in the email body (placeholders supported).

        footer (str | None):
            Text block below the log in the email body (placeholders supported).

        register_atexit (bool):
            If True → atexit is registered (if _should_register_atexit allows).
            In debugger, atexit is automatically NOT registered.

        max_buffer (int | None):
            Threshold at which the handler sends the email immediately.
            None → send only at program end (or via flush()).

        send_if_empty (bool):
            If True → send an email even without log entries.

        attachment_min_level (int | None):
            Minimum log level for the attachment.
            Example:
                level=WARNING              → mail contains WARNING+
                attachment_min_level=DEBUG → attachment contains DEBUG+ (full log)
            None → no attachment.
    """

    def __init__(
        self,
        smtp_host: str,
        smtp_port: int = 587,
        *,
        username: Optional[str] = None,
        password: Optional[str] = None,
        use_tls: bool = True,
        use_ssl: bool = False,
        timeout: Optional[float] = 10.0,
        from_addr: str,
        to_addrs: Union[str, Sequence[str]],
        cc_addrs: Optional[Union[str, Sequence[str]]] = None,
        bcc_addrs: Optional[Union[str, Sequence[str]]] = None,
        subject_template: str = "Logs ({levelname_max}) from {program_name} @ {hostname} – {count} entries",
        header: Optional[str] = None,
        footer: Optional[str] = None,
        register_atexit: bool = True,
        max_buffer: Optional[int] = None,
        send_if_empty: bool = False,
        attachment_min_level: Optional[int] = logging.DEBUG,
    ):
        super().__init__(level=logging.NOTSET)  # Level controlled via handler.setLevel()
        if use_ssl and use_tls:
            raise ValueError("use_ssl and use_tls are mutually exclusive. Please set only one.")

        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.username = username
        self.password = password
        self.use_tls = use_tls
        self.use_ssl = use_ssl
        self.timeout = timeout

        self.from_addr = from_addr
        self.to_addrs = _ensure_list(to_addrs)
        self.cc_addrs = _ensure_list(cc_addrs)
        self.bcc_addrs = _ensure_list(bcc_addrs)

        self.subject_template = subject_template
        self.header = header
        self.footer = footer

        self.max_buffer = max_buffer
        self.send_if_empty = send_if_empty

        # Buffer for mail body (only entries >= handler.level)
        self._mail_buffer: List[str] = []
        # Buffer for full log (attachment) from attachment_min_level upward
        self._full_buffer: List[str] = []
        # Level from which log goes into attachment (None = no attachment)
        self.attachment_min_level: Optional[int] = attachment_min_level

        self._highest_levelno: int = 0
        self._lock = threading.RLock()
        self._closed = False

        # atexit registration (disabled in debugger)
        self._atexit_token = None
        if _should_register_atexit(register_atexit):
            def _cb():
                self._atexit_send()
            self._atexit_token = _cb
            atexit.register(_cb)

    # -----------------------------------------------------------------------
    # Logging.Handler API
    # -----------------------------------------------------------------------
    def emit(self, record: logging.LogRecord) -> None:
        """
        Formats each record as a single line and buffers it.
        Mail body: only records >= handler.level
        Attachment: records >= attachment_min_level (if set)
        """
        try:
            msg_line = self._format_record(record)
            with self._lock:
                # 1) Collect full log for attachment (optional)
                if self.attachment_min_level is not None and record.levelno >= self.attachment_min_level:
                    self._full_buffer.append(msg_line)

                # 2) Mail body filtered by normal handler level
                if record.levelno >= self.level:
                    self._mail_buffer.append(msg_line)
                    if record.levelno > self._highest_levelno:
                        self._highest_levelno = record.levelno

                # Optional: early sending when buffer threshold reached
                if self.max_buffer is not None and len(self._mail_buffer) >= self.max_buffer:
                    self._send_locked()
        except Exception:
            # Never raise exceptions outward (logging must remain robust)
            try:
                sys.stderr.write("BufferedSMTPHandler.emit(): Error while buffering.\n")
            except Exception:
                pass

    def flush(self) -> None:
        """
        Manual sending of currently buffered messages (if any).
        """
        with self._lock:
            self._send_locked()

    def close(self) -> None:
        """
        Sending when closing the handler (called by logging.shutdown() at program end).
        Robust against late interrupts; deregisters the atexit callback.
        """
        try:
            with self._lock:
                # Deregister atexit callback (if present)
                if self._atexit_token is not None:
                    try:
                        atexit.unregister(self._atexit_token)
                    except Exception:
                        pass
                    self._atexit_token = None

                if not self._closed:
                    try:
                        self._send_locked()
                    except BaseException as exc:
                        # Suppress KeyboardInterrupt/SystemExit during shutdown
                        try:
                            sys.stderr.write(f"BufferedSMTPHandler.close(): ignored {type(exc).__name__} during shutdown.\n")
                        except Exception:
                            pass
                    self._closed = True
        finally:
            super().close()

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------
    def _format_record(self, record: logging.LogRecord) -> str:
        """
        Uses the handler's formatter if set; otherwise a solid default.
        """
        fmt = self.formatter
        if fmt is None:
            # Default formatter: timestamp, level, loggername:line – message
            fmt = logging.Formatter(
                fmt="%(asctime)s %(levelname)s [%(name)s:%(lineno)d] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S"
            )
        return fmt.format(record)

    def _make_subject(self, count: int) -> str:
        program_name = os.path.basename(sys.argv[0]) if sys.argv else "<program>"
        hostname = socket.gethostname()
        highest_levelno = self._highest_levelno or logging.NOTSET
        levelname_max = logging.getLevelName(highest_levelno)
        min_level = logging.getLevelName(self.level)

        context = {
            "count": count,
            "levelname_max": levelname_max,
            "levelno_max": highest_levelno,
            "program_name": program_name,
            "hostname": hostname,
            "date": datetime.now(),
            "min_level": min_level,
        }
        try:
            return self.subject_template.format(**context)
        except Exception:
            # Fallback without placeholder processing
            return f"Logs ({levelname_max}) {program_name}@{hostname} – {count}"

    def _make_body(self, lines: List[str]) -> str:
        parts = []
        if self.header:
            try:
                parts.append(self.header.format(
                    count=len(lines),
                    levelname_max=logging.getLevelName(self._highest_levelno or logging.NOTSET),
                    levelno_max=(self._highest_levelno or logging.NOTSET),
                    program_name=os.path.basename(sys.argv[0]) if sys.argv else "<program>",
                    hostname=socket.gethostname(),
                    date=datetime.now(),
                    min_level=logging.getLevelName(self.level),
                ))
            except Exception:
                parts.append(self.header)

        if lines:
            parts.append("\n".join(lines))

        if self.footer:
            try:
                parts.append(self.footer.format(
                    count=len(lines),
                    levelname_max=logging.getLevelName(self._highest_levelno or logging.NOTSET),
                    levelno_max=(self._highest_levelno or logging.NOTSET),
                    program_name=os.path.basename(sys.argv[0]) if sys.argv else "<program>",
                    hostname=socket.gethostname(),
                    date=datetime.now(),
                    min_level=logging.getLevelName(self.level),
                ))
            except Exception:
                parts.append(self.footer)

        return "\n\n".join(parts) if parts else ""

    def _send_locked(self) -> None:
        """
        Sends the buffered entries (thread-safe).
        Expects: self._lock held.
        """
        # If body empty and send_if_empty False → no sending
        if not self._mail_buffer and not self.send_if_empty:
            return

        mail_lines = list(self._mail_buffer)
        attach_lines = list(self._full_buffer)
        subject = self._make_subject(len(mail_lines))
        body = self._make_body(mail_lines)
        recipients = self.to_addrs + self.cc_addrs + self.bcc_addrs

        # After creating the message, reset buffers so nothing is sent twice
        self._mail_buffer.clear()
        self._full_buffer.clear()
        self._highest_levelno = 0

        try:
            msg = EmailMessage()
            msg["From"] = self.from_addr
            msg["To"] = ", ".join(self.to_addrs) if self.to_addrs else self.from_addr
            if self.cc_addrs:
                msg["Cc"] = ", ".join(self.cc_addrs)
            msg["Subject"] = subject
            # UTF-8 body
            msg.set_content(body or "(No contents)", subtype="plain", charset="utf-8")

            # Attachment (if present)
            if self.attachment_min_level is not None and len(attach_lines) > 0:
                attachment_text = "\n".join(attach_lines) or "(No debug logs available)"
                msg.add_attachment(
                    attachment_text.encode("utf-8"),
                    maintype="text",
                    subtype="plain",
                    filename="full_log.txt"
                )

            if not recipients:
                recipients = [self.from_addr]

            # Connection setup
            if self.use_ssl:
                server = smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, timeout=self.timeout)
            else:
                server = smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=self.timeout)

            with server:
                # Optional: debug controlled via environment variables
                try:
                    if os.environ.get("SMTP_DEBUG", "") == "1":
                        server.set_debuglevel(1)
                except Exception:
                    pass

                server.ehlo()
                if self.use_tls and not self.use_ssl:
                    server.starttls()
                    server.ehlo()

                if self.username:
                    server.login(self.username, self.password or "")

                server.send_message(msg, from_addr=self.from_addr, to_addrs=recipients)

        except Exception as exc:
            # Sending errors must not crash the program.
            try:
                sys.stderr.write(f"BufferedSMTPHandler: sending error: {exc}\n")
            except Exception:
                pass

    def _atexit_send(self) -> None:
        """
        Fallback layer for sending via atexit.
        Does not raise hard errors if the interpreter is already finalizing
        or KeyboardInterrupt/SystemExit occurs.
        """
        try:
            with self._lock:
                # If interpreter already finalizing, do not attempt IO
                if getattr(sys, "is_finalizing", False):
                    return

                if not self._closed and (self._mail_buffer or self.send_if_empty):
                    try:
                        self._send_locked()
                    except BaseException as exc:
                        # Suppress KeyboardInterrupt/SystemExit cleanly
                        try:
                            sys.stderr.write(f"BufferedSMTPHandler._atexit_send(): ignored {type(exc).__name__} ({exc})\n")
                        except Exception:
                            pass
                    self._closed = True
        except Exception:
            try:
                sys.stderr.write("BufferedSMTPHandler._atexit_send(): Error during final send (suppressed).\n")
            except Exception:
                pass


# -----------------------------------------------------------------------------
# Convenience function: create handler
# -----------------------------------------------------------------------------
def make_email_handler(
    *,
    smtp_host: str,
    smtp_port: int = 587,
    from_addr: str,
    to_addrs: Union[str, Sequence[str]],
    username: Optional[str] = None,
    password: Optional[str] = None,
    use_tls: bool = True,
    use_ssl: bool = False,
    timeout: Optional[float] = 10.0,
    cc_addrs: Optional[Union[str, Sequence[str]]] = None,
    bcc_addrs: Optional[Union[str, Sequence[str]]] = None,
    subject_template: str = "Logs ({levelname_max}) from {program_name} @ {hostname} – {count} entries",
    header: Optional[str] = None,
    footer: Optional[str] = None,
    register_atexit: bool = True,
    max_buffer: Optional[int] = None,
    send_if_empty: bool = False,
    level: int = logging.INFO,                            # Default threshold (adjustable)
    attachment_min_level: Optional[int] = logging.DEBUG,  # None => no attachment
    formatter: Optional[logging.Formatter] = None,
) -> BufferedSMTPHandler:
    """
    Creates the handler with typical configuration and sets level & formatter.
    """
    handler = BufferedSMTPHandler(
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        username=username,
        password=password,
        use_tls=use_tls,
        use_ssl=use_ssl,
        timeout=timeout,
        from_addr=from_addr,
        to_addrs=to_addrs,
        cc_addrs=cc_addrs,
        bcc_addrs=bcc_addrs,
        subject_template=subject_template,
        header=header,
        footer=footer,
        register_atexit=register_atexit,
        attachment_min_level=attachment_min_level,
        max_buffer=max_buffer,
        send_if_empty=send_if_empty,
    )
    handler.setLevel(level)
    if formatter is not None:
        handler.setFormatter(formatter)
    return handler


# -----------------------------------------------------------------------------
# Example usage (for quick testing via direct execution)
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    logger = logging.getLogger("demo")
    logger.setLevel(TRACE)  # Entire system may log up to TRACE

    # Optional: custom formatter for mail lines
    mail_formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s:%(lineno)d | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    handler = make_email_handler(
        smtp_host="smtp.example.com",
        smtp_port=587,
        use_tls=True,
        use_ssl=False,
        username="user@example.com",
        password="***PASSWORD***",
        from_addr="noreply@example.com",
        to_addrs=["receiver@example.com"],
        subject_template="[{levelname_max}] {program_name} @ {hostname} – {count} entries (from {min_level})",
        header=(
            "Hello team,\n\n"
            "here is the summarized log report:\n"
            "Program: {program_name}\nHost: {hostname}\n"
            "Time: {date:%Y-%m-%d %H:%M:%S}\n"
            "Threshold: {min_level}\n"
            "Count: {count}\n"
        ),
        footer=(
            "\n---\n"
            "Automatically generated. Highest level: {levelname_max} (#{levelno_max})\n"
            "Best regards"
        ),
        level=logging.WARNING,                 # only WARNING and above in the mail
        attachment_min_level=logging.DEBUG,    # messages from DEBUG upward as attachment.
        formatter=mail_formatter,
        max_buffer=None,             # optional: e.g. send immediately at 500 entries
        register_atexit=True,        # automatically send mail at program end (off in debugger)
    )

    logger.addHandler(handler)

    # Example logs
    logger.trace("This is a TRACE (only visible if handler level <= TRACE).")
    logger.debug("Debug info (not included in mail at level=WARNING, thus only in attachment).")
    logger.info("All ready.")
    logger.warning("Attention, something looks suspicious (Message included in mail body).")
    logger.error("An error occurred.")
    logger.critical("Critical error, check immediately!")

    # No further call needed – sending happens automatically at the end.
    # Alternatively, manually:
    # handler.flush()
    sys.exit(0)
