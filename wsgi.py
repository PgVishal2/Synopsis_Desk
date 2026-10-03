"""Entry point for gunicorn / flask:  gunicorn wsgi:app"""
from webapp import create_app

app = create_app()
