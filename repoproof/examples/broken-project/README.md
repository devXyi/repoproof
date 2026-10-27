# Broken Project

A tiny demo app whose README has the kinds of problems RepoProof catches. RepoProof's tests
and docs use it as a fixture, so please do not copy its setup instructions.

## Setup

```bash
python3 -m venv .venv
source venv/bin/activate
cp .env.example .env
```

## Run

```bash
python3 app.py
```

The full walkthrough lives in [the setup guide](docs/setup.md). See
[Installation](#installation) for platform notes and [Configuration](#configuration) for the
settings below.

## Configuration

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | Where the app stores its data |
