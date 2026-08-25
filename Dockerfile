FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PORT=8080

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --chmod garantit des fichiers lisibles quelles que soient les perms du contexte
# de build (les sources en 0600 rendaient /srv/app illisible pour l'utilisateur non-root).
COPY --chmod=0644 app ./app

# Pas de root dans le conteneur.
RUN useradd --system --uid 1001 appuser \
 && chmod -R a+rX /srv/app
USER appuser

EXPOSE 8080
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1"]
