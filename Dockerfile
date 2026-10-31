# --- Assets stage: minify the static JS/CSS for deploy -----------------
# Repo source (src/static/*) stays fully readable for development/review;
# only this build's output gets shipped. See scripts/build_assets.sh for
# why (short version: any client-side code is visible in a browser's
# view-source no matter what, always has been, always will be for any
# website - minifying just avoids handing out a nicely commented tutorial
# alongside it; it is not a substitute for keeping secrets server-side,
# which this app already does).
FROM node:20-slim AS assets
WORKDIR /app
COPY package.json .
RUN npm install --no-save esbuild
COPY scripts/build_assets.sh scripts/build_assets.sh
COPY src/static src/static
RUN bash scripts/build_assets.sh

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install system dependencies (gcc needed to build some Python wheels)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .
# Overwrite the readable static source with the minified build from above.
COPY --from=assets /app/src/static src/static

# Create non-root user
RUN useradd -m -u 1000 bingo && chown -R bingo:bingo /app
USER bingo

EXPOSE 8000

# All persistence is Postgres via Supabase (see .env.example), so there
# is no local database to wait for - the container just runs the app.
CMD ["python", "main.py", "api"]
