echo "Running Alembic migrations..."
echo "  pwd: $(pwd)"

# Show the real reason if this fails (don't redirect to /dev/null)
if ! python -m alembic --version; then
  echo "ERROR: python -m alembic failed (alembic is installed but not runnable)." >&2
  echo "  python path: $(command -v python)" >&2
  python -V >&2
  python -m pip show alembic >&2 || true
  exit 1
fi

echo "  cmd: python -m alembic -c alembic.ini upgrade head"
python -m alembic -c alembic.ini upgrade head
