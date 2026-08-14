"""
The Flask app instance itself, kept in its own module so routes.py can
import it without a circular import back to __init__.py.
"""
import threading

from flask import Flask

app = Flask(__name__)
state_lock = threading.Lock()
