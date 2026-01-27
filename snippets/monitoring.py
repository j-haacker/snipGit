__all__ = ["mail_traceback_wrapper"]

from contextlib import contextmanager
from email.message import EmailMessage
from pathlib import Path
import smtplib
import traceback
import sys


def mail_traceback_wrapper(
    to_address: str, subject_tag: str = None, from_address: str = None
):
    # CREDIT: FBruzzesi, Nathan Davis https://stackoverflow.com/a/27500036
    def decorate(f):
        def applicator(*args, **kwargs):
            try:
                return f(*args, **kwargs)
            except Exception as err:
                msg = EmailMessage()
                msg["To"] = to_address
                subject = str(err).strip().replace("\n", "; ")
                if subject_tag is not None and subject_tag != "":
                    subject = f"[{subject_tag.strip()}] {subject}"
                msg["Subject"] = subject
                if from_address is not None:
                    msg["From"] = from_address.replace(" ", "_")
                msg.set_content(traceback.format_exc())
                print(str(err).strip().replace("\n", "; "), msg)
                with smtplib.SMTP("localhost") as s:
                    s.send_message(msg)
                raise

        return applicator

    return decorate


@contextmanager
def pipe_output_to_logfile(path: Path | str):
    with open(path, "a") as lf:
        sys.stdout = lf
        sys.stderr = lf
