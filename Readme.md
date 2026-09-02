# E-Mail loghandler
An extended logging handler that collects log entries and automatically sends
them via email at program termination. In addition to the normal email body,
which is filtered according to the configured logging level, a complete log
history (e.g., from DEBUG level upward) can optionally be sent as a UTF‑8
encoded file attachment.

The email fully supports UTF‑8 (including special characters, Unicode, and emojis),
both in the message text and in the attachment.

The email is automatically sent by default:
 - at the end of the Python interpreter via *logging.shutdown()* → *handler.close()*
 - (optionally) additionally via an *atexit callback* (not in debug mode)
Therefore, no explicit function call is required for sending.

Working with Python 3.8 and above, actual tested with Python 3.14

### Difference to existing projects
The main difference to the standard [SMTPHandler](https://docs.python.org/3/library/logging.handlers.html#smtphandler)
is, that this handler does not send a seperate email for every log message.

## Main features:
- Buffering of log entries until program termination
- Sending an email whose content is filtered according to handler.level
- Optional: full log history as UTF‑8 file attachment ("full_log.txt")
- Freely definable subject, header, and footer (with placeholders)
- Configurable trace/minimum level
- SSL or TLS SMTP support
- Optional: send when a buffer threshold (max_buffer) is reached
- Robust error handling (logging must not break the program)
- Debug-robust: no atexit in debugger, `close()` catches BaseException (KeyboardInterrupt/SystemExit)

## Usage
### First test
For a first test you can just download the *email_loghandler.py*-script, replace the call of `handler = make_email_handler(...)`
at the end of the file with valid cedentials for your email account.
Then run `python email_loghandler.py` and you should receive an email.

### Installation
You can install the email_loghandler modul with pip:
```shell
pip install git+ssh://git@github.com/jstiete/email_loghandler
```
Or with any other packet manager (like uv):
```shell
uv add git+ssh://git@github.com/jstiete/email_loghandler
```
Or using https instead of ssh:
```shell
pip install git+https://github.com/jstiete/email_loghandler
uv add git+https://github.com/jstiete/email_loghandler
```

### Usage in your script
```Python
import logging
from email_log_handler import make_email_handler, TRACE

logger = logging.getLogger("my_app_logger")
logger.setLevel(TRACE)      # Globally allow TRACE and above

handler = make_email_handler(
    smtp_server="smtp.example.com",
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
```

### Use config file for email credentials
`make_email_handler()` can be configured entirely through a ConfigParser object, allowing SMTP credentials,
server settings, logging thresholds, and formatting options to be defined in an external configuration file.  
**Any values present in the config file override the function arguments!**

This makes the handler easy to integrate into applications where email settings should not be hard‑coded,
such as production systems, shared environments, or tools that rely on user‑provided configuration.

```ini
## config.ini file

[EMAIL_LOGHANDLER]
smtp_server=smtp.example.com
smtp_port=587
use_tls=True
username=username@example.com
password=***PASSWORD***
from_addr=username@example.com
to_addrs=receiver@example.com, receiver_2@example.com
```

```python
import logging
from email_log_handler import make_email_handler, TRACE
from configparser import ConfigParser

logger = logging.getLogger("my_app_logger")
logger.setLevel(TRACE)      # Globally allow TRACE and above

# Load configuration file
config = ConfigParser()
config.read("config.ini")

handler = make_email_handler(
    config=config,
    smtp_server="This will be overwritten by config",
    subject_template="[Log-Report] {program_name} – {count} entries (max: {levelname_max})",
    header="Hello {program_name} user,\n\nhere is the log excerpt:\n",
    footer="\n--\nAutomatic dispatch @ {hostname} / {date:%Y-%m-%d %H:%M}",
    level=logging.WARNING,               # From WARNING upward in the mail body
    attachment_min_level=logging.DEBUG,  # But send full log from DEBUG upward as attachment
)

logger.addHandler(handler)

logger.info("All ready.")
```

## Placeholders for `subject_template`, `header`, `footer`:
| Placeholder      | Description                              |
|------------------|------------------------------------------|
| \{count}         | Number of entries in the email body      |
| \{levelname_max} | Highest occurring log level name         |
| \{levelno_max}   | Highest occurring level number           |
| \{program_name}  | Current script/program name              |
| \{hostname}      | Machine hostname                         |
| \{date}          | Sending timestamp (datetime object)      |
| \{min_level}     | Current filter level of the email body   |

## Parameters:
    smtp_server (str):
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

    ssl_context (ssl.SSLContext | None):
        User-defined SSL context for the SMTP connection.
        If None, a default SSL context is created using certifi.

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
        Subject template supporting placeholders.

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

## Debugging
You can run a local SMTP debugging server for testing email functionality with the
[aiosmtpd](https://pypi.org/project/aiosmtpd/) module.
Visit the linked Homepage for detailed information.<br>
Instead of sending real emails, this server prints the content to the console.
You don't need to worry about encryption or credential to log in your email server.

You can install the aiosmtpd module with pip:
```shell
$ python -m pip install aiosmtpd
```
Then start aiosmtpd in a second console window:
```shell
$ python -m aiosmtpd -n
```
The server runs by default on localhost, at port 8025.
Any emails sent to this server are printed to the terminal.
The debug server doesn’t implement any authentication or security,
making it perfect for debugging.

email_loghandler configuration for aiosmtpd:
```python
smtp_server = "localhost"
smtp_port = 8025
sender_email = "me@example.com"
receiver_email = "you@example.com"
```

## License
This Program is licensed under MIT license.
