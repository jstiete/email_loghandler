# __init__.py

from .email_loghandler import BufferedSMTPHandler
from .email_loghandler import make_email_handler

__all__ = ["BufferedSMTPHandler",
           "make_email_handlern",
           ]